"""
GStreamer Pipeline — rebuilt on the exact pattern of working test_webrtc.py.

attach_peer() is a direct port of test_webrtc.py's attach_peer(), with:
  - add-transceiver(SENDONLY, caps) before any pad linking
  - on-negotiation-needed signal (not a timer)
  - set-local-description with promise.interrupt() (exactly as test_webrtc.py)
  - set-remote-description with Gst.Promise.new()  (exactly as test_webrtc.py)
  - GLib.idle_add for all webrtcbin calls from asyncio thread

Additional sources beyond videotestsrc:
  - udp://   raw RTP/H264 UDP stream
  - rtsp://  RTSP stream
  - KLV      side-channel UDP listener (MISB ST 0601)
"""

import os
os.environ["G_MESSAGES_DEBUG"] = ""

import gi
gi.require_version("Gst",       "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp",    "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import logging, threading, re
from typing import Optional, Dict, Callable
from dataclasses import dataclass

try:
    from klv_parser import parse_klv_from_buffer
    HAS_KLV = True
except ImportError:
    HAS_KLV = False

logger = logging.getLogger("pipeline")


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class StreamConfig:
    stream_id: str
    uri:       str
    name:      str = ""
    width:     int = 1280
    height:    int = 720
    klv_port:  Optional[int] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fix_answer(sdp: str) -> str:
    """packetization-mode=0 → 1 (GStreamer encodes mode=1 always)."""
    return re.sub(r'packetization-mode=0', 'packetization-mode=1', sdp)


def _fix_sdp(sdp: str) -> str:
    """
    Strip the duplicate m=video section GStreamer emits (video1/bundle-only)
    and force a=sendonly on video0.
    GStreamer always emits two m=video when request_pad_simple is used —
    video0 (the real one, sendonly) and video1 (bundle-only, sendrecv, port=0).
    Chrome rejects the offer if both are present.
    """
    lines = sdp.splitlines()
    out: list[str] = []
    skip = False

    for line in lines:
        # Start of a new m= section
        if line.startswith("m="):
            # If this m= has port 0 it's the bundle-only dummy — skip it
            skip = (line.split()[1] == "0")
            if not skip:
                out.append(line)
            continue

        if skip:
            continue

        # Force sendonly direction on the real video section
        if line.strip() in ("a=sendrecv", "a=recvonly", "a=inactive"):
            out.append("a=sendonly")
            continue

        # Drop rtcp-mux-only — causes Chrome to respond with port=0
        if line.strip() == "a=rtcp-mux-only":
            continue

        # Force profile-level-id=42c01f so Chrome decodes constrained-baseline
        if line.strip().startswith("a=fmtp:") and "profile-level-id=" in line:
            line = re.sub(r"profile-level-id=[0-9a-fA-F]+",
                          "profile-level-id=42c01f", line)

        out.append(line)

    # Fix a=group:BUNDLE to only list video0
    result_lines: list[str] = []
    for line in out:
        if line.strip().startswith("a=group:BUNDLE"):
            result_lines.append("a=group:BUNDLE video0")
        else:
            result_lines.append(line)

    result = "\r\n".join(result_lines)
    if not result.endswith("\r\n"):
        result += "\r\n"
    return result


# ── StreamPipeline ────────────────────────────────────────────────────────────

class StreamPipeline:
    """
    One pipeline per stream URI.
    Peer management is a direct port of test_webrtc.py's Pipeline + attach_peer().
    """

    def __init__(self, config: StreamConfig,
                 on_klv:   Callable,
                 on_error: Callable):
        self.config   = config
        self.on_klv   = on_klv
        self.on_error = on_error

        self.pipeline      = None
        self.tee           = None
        self._klv_pipeline = None
        self._lock         = threading.Lock()

        # peer_id → {"wb": webrtcbin, "q": queue, "tee_src": pad,
        #             "send_fn": callable, "cleanup": callable}
        self.peers: Dict[str, dict] = {}

    # ── Build pipeline string ─────────────────────────────────────────────────

    def _build(self) -> str:
        sid = self.config.stream_id
        uri = self.config.uri

        # All non-test sources: decode → re-encode as H264 constrained-baseline
        # so the output is always compatible with browsers regardless of source.
        encode = (
            "! videoconvert ! video/x-raw,format=I420 "
            "! x264enc tune=zerolatency bitrate=2000 speed-preset=ultrafast "
            "    key-int-max=30 bframes=0 "
            "! video/x-h264,profile=constrained-baseline,stream-format=byte-stream "
            "! h264parse config-interval=-1 "
        )

        if uri.startswith("test://"):
            # ── identical to test_webrtc.py ──────────────────────────────────
            return (
                "videotestsrc pattern=18 is-live=true "
                "! video/x-raw,width=640,height=360,framerate=30/1 "
                "! videoconvert ! x264enc tune=zerolatency bitrate=500 "
                "! rtph264pay config-interval=-1 pt=96 "
                f"! tee name=tee_{sid} allow-not-linked=true "
                f"tee_{sid}. ! queue ! fakesink sync=false"
            )

        elif uri.startswith("udp://"):
            port = uri.split(":")[-1] if ":" in uri else "5004"
            return (
                f'udpsrc address=0.0.0.0 port={port} '
                f'caps="application/x-rtp,media=video,clock-rate=90000,'
                f'encoding-name=H264,payload=96" '
                f'! rtpjitterbuffer latency=200 drop-on-latency=true '
                f'! rtph264depay '
                f'! h264parse config-interval=-1 '
                f'! rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 '
                f'! tee name=tee_{sid} allow-not-linked=true '
                f'tee_{sid}. ! queue ! fakesink sync=false'
            )

        elif uri.startswith("rtsp://"):
            src = (
                f"rtspsrc location={uri} latency=200 protocols=tcp "
                f"! rtph264depay ! h264parse ! avdec_h264 "
            )

        else:
            src = f"uridecodebin uri={uri} latency=200 "

        return (
            f"{src}"
            f"{encode}"
            f"! rtph264pay config-interval=-1 pt=96 "
            f"! tee name=tee_{sid} allow-not-linked=true "
            f"tee_{sid}. ! queue ! fakesink sync=false"
        )

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(self):
        Gst.init(None)
        pipe_str = self._build()
        logger.info(f"[{self.config.stream_id}] Pipeline:\n{pipe_str}")

        self.pipeline = Gst.parse_launch(pipe_str)
        self.tee      = self.pipeline.get_by_name(f"tee_{self.config.stream_id}")

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        self.pipeline.set_state(Gst.State.PLAYING)
        self.pipeline.get_state(Gst.SECOND * 5)
        logger.info(f"[{self.config.stream_id}] PLAYING")

        if self.config.klv_port and HAS_KLV:
            self._start_klv(self.config.klv_port)

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self._klv_pipeline:
            self._klv_pipeline.set_state(Gst.State.NULL)

    def _on_bus(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            logger.error(f"[{self.config.stream_id}] GST ERROR: {err} | {dbg}")
            self.on_error(self.config.stream_id, str(err))
        elif msg.type == Gst.MessageType.WARNING:
            warn, _ = msg.parse_warning()
            logger.warning(f"[{self.config.stream_id}] GST WARN: {warn}")
        elif msg.type == Gst.MessageType.EOS:
            logger.info(f"[{self.config.stream_id}] EOS — pipeline ended")

    # ── KLV side-channel ─────────────────────────────────────────────────────

    def _start_klv(self, port: int):
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
            logger.info(f"[{sid}] KLV listener on UDP port {port}")
        except Exception as e:
            logger.warning(f"[{sid}] KLV start failed: {e}")

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

    # ── Peer management — direct port of test_webrtc.py attach_peer() ────────

    def attach_peer(self, peer_id: str, send_fn: Callable) -> bool:
        """
        Attach a new WebRTC peer.  send_fn(msg_dict) sends JSON to the browser.
        Returns True on success.

        This is a line-for-line port of the working test_webrtc.py attach_peer(),
        with the only additions being:
          - GLib.idle_add wrappers for calls that arrive from the asyncio thread
          - tee linkage instead of a fixed appsrc
        """
        sid = self.config.stream_id

        q  = Gst.ElementFactory.make("queue",      None)
        wb = Gst.ElementFactory.make("webrtcbin",  None)
        if not q or not wb:
            logger.error(f"[{sid}] Could not create queue/webrtcbin elements")
            return False

        wb.set_property("bundle-policy", "max-bundle")

        # DO NOT call add-transceiver — it always creates a second transceiver
        # when followed by request_pad_simple, resulting in two m=video sections.
        # GStreamer creates a SENDRECV transceiver automatically from request_pad_simple.
        # We strip the duplicate video1 section in _fix_sdp before sending to browser.

        self.pipeline.add(q)
        self.pipeline.add(wb)
        q.get_static_pad("src").link(wb.request_pad_simple("sink_%u"))
        tee_src = self.tee.request_pad_simple("src_%u")
        tee_src.link(q.get_static_pad("sink"))

        # ── Offer callback ────────────────────────────────────────────────────
        offer_sent = [False]

        def _on_offer(promise, element, _):
            promise.wait()
            reply = promise.get_reply()
            if not reply:
                return
            offer = reply.get_value("offer")
            if not offer:
                return
            sdp_text = offer.sdp.as_text()
            sdp_text = _fix_sdp(sdp_text)  # strip video1, fix direction
            set_ld = Gst.Promise.new()
            element.emit("set-local-description", offer, set_ld)
            set_ld.interrupt()
            logger.info(f"[{sid}][{peer_id}] SDP offer →\n{sdp_text}")
            send_fn({"type": "offer", "sdp": sdp_text})

        def _on_negotiation_needed(element):
            if offer_sent[0]:
                return
            offer_sent[0] = True
            logger.info(f"[{sid}][{peer_id}] on-negotiation-needed → create-offer")
            promise = Gst.Promise.new_with_change_func(_on_offer, element, None)
            element.emit("create-offer", None, promise)

        # 3. Connect signals BEFORE sync_state so we don't miss on-negotiation-needed
        wb.connect("on-negotiation-needed", _on_negotiation_needed)
        wb.connect(
            "on-ice-candidate",
            lambda e, idx, cand: send_fn(
                {"type": "ice", "candidate": cand, "sdpMLineIndex": idx}
            )
        )

        # 4. sync_state fires on-negotiation-needed — signal is now connected
        q.sync_state_with_parent()
        wb.sync_state_with_parent()

        # ── 5. Cleanup helper ─────────────────────────────────────────────────
        def cleanup():
            try:
                wb.set_state(Gst.State.NULL)
                q.set_state(Gst.State.NULL)
                self.pipeline.remove(wb)
                self.pipeline.remove(q)
                self.tee.release_request_pad(tee_src)
            except Exception as ex:
                logger.warning(f"[{sid}][{peer_id}] cleanup error: {ex}")

        # ── 6. Answer / ICE handlers (called from asyncio → must use idle_add) ─
        def set_answer(sdp: str):
            # Fix packetization-mode before GStreamer sees the answer
            sdp = _fix_answer(sdp)
            def _apply():
                _, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp)
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg)
                # Exactly as test_webrtc.py: plain Promise.new()
                wb.emit("set-remote-description", answer, Gst.Promise.new())
                logger.info(f"[{sid}][{peer_id}] answer applied")
                return False
            GLib.idle_add(_apply)

        def add_ice(candidate: str, sdp_mline_index: int):
            if not candidate:
                logger.info(f"[{sid}][{peer_id}] ICE end-of-candidates")
                return
            logger.info(f"[{sid}][{peer_id}] ICE adding: {candidate[:80]}")
            def _apply():
                wb.emit("add-ice-candidate", sdp_mline_index, candidate)
                return False
            GLib.idle_add(_apply)

        with self._lock:
            self.peers[peer_id] = {
                "wb":        wb,
                "q":         q,
                "tee_src":   tee_src,
                "set_answer": set_answer,
                "add_ice":    add_ice,
                "cleanup":    cleanup,
            }

        logger.info(f"[{sid}] Peer {peer_id} attached")
        return True

    def detach_peer(self, peer_id: str):
        with self._lock:
            peer = self.peers.pop(peer_id, None)
        if peer:
            peer["cleanup"]()
            logger.info(f"[{self.config.stream_id}] Peer {peer_id} detached")


# ── PipelineManager ───────────────────────────────────────────────────────────

class PipelineManager:

    def __init__(self, on_klv: Callable, send_signaling: Callable,
                 use_gpu: bool = False):
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
        # Auto-assign KLV port for UDP streams (RTP port + 1)
        if config.klv_port is None and config.uri.startswith("udp://"):
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
            logger.error(f"Failed to start stream {config.stream_id}: {e}")
            return False

    def remove_stream(self, sid: str):
        pipe = self.streams.pop(sid, None)
        if pipe:
            pipe.stop()

    # ── Peer API ─────────────────────────────────────────────────────────────

    def add_peer(self, peer_id: str, stream_id: str):
        """Returns the peer dict (with set_answer/add_ice) or None."""
        pipe = self.streams.get(stream_id)
        if not pipe:
            return None

        def send_fn(msg: dict):
            self.send_signaling(peer_id, msg)

        ok = pipe.attach_peer(peer_id, send_fn)
        return pipe.peers.get(peer_id) if ok else None

    def remove_peer(self, peer_id: str, stream_id: str):
        pipe = self.streams.get(stream_id)
        if pipe:
            pipe.detach_peer(peer_id)

    def handle_peer_sdp(self, peer_id: str, stream_id: str, sdp: str):
        pipe = self.streams.get(stream_id)
        if pipe:
            peer = pipe.peers.get(peer_id)
            if peer:
                peer["set_answer"](sdp)

    def handle_peer_ice(self, peer_id: str, stream_id: str,
                        candidate: str, mline: int):
        pipe = self.streams.get(stream_id)
        if pipe:
            peer = pipe.peers.get(peer_id)
            if peer:
                peer["add_ice"](candidate, mline)

    def get_stream_list(self):
        return [
            {"stream_id": sid, "uri": p.config.uri, "name": p.config.name}
            for sid, p in self.streams.items()
        ]

    def _on_error(self, sid: str, err: str):
        logger.error(f"[{sid}] pipeline error: {err}")
