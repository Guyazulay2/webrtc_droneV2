"""
GStreamer Pipeline - Working Version
Single pipeline, WebRTC H.264
"""
import os
os.environ["G_MESSAGES_DEBUG"] = ""

import gi
gi.require_version("Gst",       "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp",    "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import logging, threading
from typing import Optional, Dict, Callable
from dataclasses import dataclass

try:
    from klv_parser import parse_klv_from_buffer
    HAS_KLV = True
except ImportError:
    HAS_KLV = False

logger = logging.getLogger("pipeline")


@dataclass
class StreamConfig:
    stream_id: str
    uri:       str
    name:      str = ""
    width:     int = 1280
    height:    int = 720
    klv_port:  Optional[int] = None


class WebRTCPeer:
    def __init__(self, peer_id, stream_id, send_cb):
        self.peer_id      = peer_id
        self.stream_id    = stream_id
        self.send_message = send_cb
        self.webrtcbin    = None
        self._offer_sent  = False

    def on_negotiation_needed(self, element):
        if self._offer_sent:
            return
        self._offer_sent = True
        logger.info(f"[{self.peer_id}] negotiation-needed → offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, element, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, webrtcbin, _):
        promise.wait()
        reply = promise.get_reply()
        if not reply:
            return
        offer = reply.get_value("offer")
        if not offer:
            return
        sdp_text = offer.sdp.as_text()
        p2 = Gst.Promise.new()
        webrtcbin.emit("set-local-description", offer, p2)
        p2.interrupt()
        logger.info(f"[{self.peer_id}] SDP offer sent")
        self.send_message(self.peer_id, {
            "type": "sdp",
            "data": {"type": "offer", "sdp": sdp_text}
        })

    def on_ice_candidate(self, _, idx, candidate):
        self.send_message(self.peer_id, {
            "type": "ice",
            "data": {"candidate": candidate, "sdpMLineIndex": idx}
        })

    def handle_sdp_answer(self, sdp_str):
        _, sdpmsg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(sdp_str.encode(), sdpmsg)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        p = Gst.Promise.new()
        self.webrtcbin.emit("set-remote-description", answer, p)
        p.interrupt()
        logger.info(f"[{self.peer_id}] SDP answer applied")

    def handle_ice_candidate(self, candidate, mline):
        # skip mDNS - Docker cannot resolve .local
        if candidate and ".local" in candidate:
            return
        self.webrtcbin.emit("add-ice-candidate", mline, candidate)


class StreamPipeline:
    def __init__(self, config: StreamConfig, on_klv, on_error):
        self.config        = config
        self.on_klv        = on_klv
        self.on_error      = on_error
        self.pipeline      = None
        self.peers:  Dict[str, WebRTCPeer] = {}
        self._lock         = threading.Lock()
        self._klv_pipeline = None

    def _build(self) -> str:
        sid = self.config.stream_id
        uri = self.config.uri

        if uri.startswith("udp://"):
            port = uri.split(":")[-1] if ":" in uri else "5004"
            src = (
                f'udpsrc address=0.0.0.0 port={port} '
                f'caps="application/x-rtp,media=video,clock-rate=90000,'
                f'encoding-name=H264,payload=96" '
                f'! rtpjitterbuffer latency=100 drop-on-latency=true '
                f'! rtph264depay ! h264parse ! avdec_h264'
            )
        elif uri.startswith("rtsp://"):
            src = (
                f"rtspsrc location={uri} latency=200 protocols=tcp "
                f"! rtph264depay ! h264parse ! avdec_h264"
            )
        else:
            src = f"uridecodebin uri={uri} latency=200"

        return f"""
            {src}
            ! videoconvert
            ! video/x-raw,format=I420
            ! x264enc tune=zerolatency bitrate=2000 speed-preset=ultrafast key-int-max=30
            ! video/x-h264,profile=baseline
            ! h264parse config-interval=1
            ! rtph264pay config-interval=1 pt=96
            ! webrtcbin name=webrtc_{sid} bundle-policy=max-bundle
        """

    def start(self):
        Gst.init(None)
        pipe_str = self._build()
        logger.info(f"[{self.config.stream_id}] Pipeline:\n{pipe_str}")
        self.pipeline = Gst.parse_launch(pipe_str)

        sid = self.config.stream_id
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        if HAS_KLV:
            it = self.pipeline.iterate_elements()
            while True:
                res, elem = it.next()
                if res != Gst.IteratorResult.OK:
                    break
                fname = elem.get_factory().get_name() if elem.get_factory() else ""
                if fname == "h264parse":
                    pad = elem.get_static_pad("sink")
                    if pad:
                        pad.add_probe(Gst.PadProbeType.BUFFER, self._klv_probe, None)
                    break

        if self.config.klv_port and HAS_KLV:
            self._start_klv(self.config.klv_port)

        self.pipeline.set_state(Gst.State.PLAYING)
        self.pipeline.get_state(Gst.SECOND * 5)
        logger.info(f"[{sid}] PLAYING")

    def _start_klv(self, port):
        sid = self.config.stream_id
        try:
            p = Gst.parse_launch(
                f"udpsrc address=0.0.0.0 port={port} "
                f"! appsink name=klvsink_{sid} emit-signals=true "
                f"sync=false max-buffers=5 drop=true"
            )
            sink = p.get_by_name(f"klvsink_{sid}")
            if sink:
                sink.connect("new-sample", self._on_klv_sample)
            p.set_state(Gst.State.PLAYING)
            self._klv_pipeline = p
            logger.info(f"[{sid}] KLV listener port {port}")
        except Exception as e:
            logger.warning(f"KLV failed: {e}")

    def _on_klv_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                klv = parse_klv_from_buffer(bytes(mi.data))
                if klv:
                    self.on_klv(self.config.stream_id, klv)
            except Exception:
                pass
            finally:
                buf.unmap(mi)
        return Gst.FlowReturn.OK

    def _klv_probe(self, pad, info, _):
        buf = info.get_buffer()
        if not buf:
            return Gst.PadProbeReturn.OK
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                klv = parse_klv_from_buffer(bytes(mi.data))
                if klv:
                    self.on_klv(self.config.stream_id, klv)
            except Exception:
                pass
            finally:
                buf.unmap(mi)
        return Gst.PadProbeReturn.OK

    def _on_bus(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            logger.error(f"[{self.config.stream_id}] {err}")
            self.on_error(self.config.stream_id, str(err))
        elif msg.type == Gst.MessageType.EOS:
            logger.info(f"[{self.config.stream_id}] EOS")

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self._klv_pipeline:
            self._klv_pipeline.set_state(Gst.State.NULL)

    def add_peer(self, peer: WebRTCPeer) -> bool:
        sid = self.config.stream_id
        wb  = self.pipeline.get_by_name(f"webrtc_{sid}")
        if not wb:
            logger.error(f"[{sid}] webrtcbin not found")
            return False
        peer.webrtcbin = wb
        wb.connect("on-negotiation-needed", peer.on_negotiation_needed)
        wb.connect("on-ice-candidate",      peer.on_ice_candidate)
        with self._lock:
            self.peers[peer.peer_id] = peer
        logger.info(f"[{sid}] Peer {peer.peer_id} added")
        GLib.timeout_add(1500, self._trigger_offer, peer, wb)
        return True

    def _trigger_offer(self, peer, wb):
        if peer._offer_sent:
            return False
        peer._offer_sent = True
        logger.info(f"[{self.config.stream_id}] Creating offer for {peer.peer_id}")
        promise = Gst.Promise.new_with_change_func(peer._on_offer_created, wb, None)
        wb.emit("create-offer", None, promise)
        return False

    def remove_peer(self, peer_id):
        with self._lock:
            self.peers.pop(peer_id, None)


class PipelineManager:
    def __init__(self, on_klv, send_signaling, use_gpu=False):
        self.on_klv         = on_klv
        self.send_signaling = send_signaling
        self.streams: Dict[str, StreamPipeline] = {}
        self._mainloop      = None

    def start_mainloop(self):
        Gst.init(None)
        self._mainloop = GLib.MainLoop()
        threading.Thread(target=self._mainloop.run, daemon=True).start()
        logger.info("GLib main loop started")

    def stop_mainloop(self):
        if self._mainloop:
            self._mainloop.quit()

    def add_stream(self, config: StreamConfig) -> bool:
        if config.stream_id in self.streams:
            return False
        if config.klv_port is None and "udp://" in config.uri:
            try:
                config.klv_port = int(config.uri.split(":")[-1]) + 1
            except Exception:
                pass
        pipe = StreamPipeline(config, self.on_klv, self._on_error)
        try:
            pipe.start()
            self.streams[config.stream_id] = pipe
            return True
        except Exception as e:
            logger.error(f"Failed: {e}")
            return False

    def remove_stream(self, sid):
        pipe = self.streams.pop(sid, None)
        if pipe:
            pipe.stop()

    def add_peer(self, peer_id, stream_id):
        pipe = self.streams.get(stream_id)
        if not pipe:
            return None
        peer = WebRTCPeer(peer_id, stream_id, self.send_signaling)
        return peer if pipe.add_peer(peer) else None

    def remove_peer(self, peer_id, stream_id):
        if stream_id in self.streams:
            self.streams[stream_id].remove_peer(peer_id)

    def handle_peer_sdp(self, peer_id, stream_id, sdp):
        pipe = self.streams.get(stream_id)
        if pipe:
            peer = pipe.peers.get(peer_id)
            if peer:
                peer.handle_sdp_answer(sdp)

    def handle_peer_ice(self, peer_id, stream_id, candidate, mline):
        pipe = self.streams.get(stream_id)
        if pipe:
            peer = pipe.peers.get(peer_id)
            if peer:
                peer.handle_ice_candidate(candidate, mline)

    def get_stream_list(self):
        return [
            {"stream_id": sid, "uri": p.config.uri, "name": p.config.name}
            for sid, p in self.streams.items()
        ]

    def _on_error(self, sid, err):
        logger.error(f"[{sid}] {err}")
