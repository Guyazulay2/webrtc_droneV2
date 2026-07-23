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

import base64, hashlib, hmac, logging, threading, re, time as _time
from typing import Optional, Dict, Callable
from dataclasses import dataclass

try:
    from klv_parser import parse_klv_from_buffer
    HAS_KLV = True
except ImportError:
    HAS_KLV = False

logger = logging.getLogger("pipeline")

# ── ICE / STUN / TURN ─────────────────────────────────────────────────────────
# GStreamer webrtcbin needs "stun://" scheme; browser API needs "stun:" scheme.
# We use the stun:// form here; main.py strips it for the browser endpoint.
_STUN_SERVER = os.getenv("STUN_SERVER", "stun://stun.l.google.com:19302")
_TURN_HOST   = os.getenv("TURN_HOST",   "")
_TURN_SECRET = os.getenv("TURN_SECRET", "")


def _turn_uri(peer_id: str) -> str:
    """Build a time-limited TURN URI using COTURN REST API (HMAC-SHA1)."""
    if not _TURN_HOST or not _TURN_SECRET:
        return ""
    expiry   = int(_time.time()) + 3600          # 1-hour window
    username = f"{expiry}:{peer_id}"
    password = base64.b64encode(
        hmac.new(_TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    return f"turn://{username}:{password}@{_TURN_HOST}:3478"


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
        # queue: unlimited buffers (0=unlimited) so frames are never dropped.
        # sync=false on fakesink so GStreamer doesn't stall on clock sync.
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
            # Passthrough: sender already outputs constrained-baseline H264 in RTP.
            # No decode/re-encode → no profile drift, no CPU waste.
            port = uri.split(":")[-1] if ":" in uri else "5004"
            return (
                f'udpsrc address=0.0.0.0 port={port} '
                f'caps="application/x-rtp,media=video,clock-rate=90000,'
                f'encoding-name=H264,payload=96" '
                f'! rtpjitterbuffer latency=100 drop-on-latency=false do-retransmission=false '
                f'! rtph264depay '
                f'! h264parse config-interval=-1 '
                f'! rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 '
                f'! tee name=tee_{sid} allow-not-linked=true '
                f'tee_{sid}. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 ! fakesink sync=false async=false'
            )

        elif uri.startswith("rtsp://"):
            src = (
                f"rtspsrc location={uri} latency=200 protocols=tcp "
                f"! rtph264depay ! h264parse ! avdec_h264 "
            )

        else:
            # TS file: filesrc → tsparse → tsdemux → h264 branch
            # Use sync=false so file plays at full speed without clock stalls.
            path = uri.replace("file://", "") if uri.startswith("file://") else uri
            src = (
                f"filesrc location={path} "
                f"! tsparse set-timestamps=true "
                f"! tsdemux name=demux demux. "
                f"! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
                f"! h264parse "
            )

        return (
            f"{src}"
            f"{encode}"
            f"! rtph264pay config-interval=-1 pt=96 "
            f"! tee name=tee_{sid} allow-not-linked=true "
            f"tee_{sid}. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 ! fakesink sync=false async=false"
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

    # ── Peer management ───────────────────────────────────────────────────────

    def attach_peer(self, peer_id: str, send_fn: Callable) -> bool:
        """
        Attach a new WebRTC peer.  send_fn(msg_dict) sends JSON to the browser.
        Returns True on success.

        All GStreamer pipeline mutations run inside GLib.idle_add() so they
        execute on the GLib main loop thread.  Calling pipeline.add / pad.link /
        sync_state_with_parent from the asyncio thread while the main loop is
        running is unsafe and was silently preventing on-negotiation-needed from
        firing.
        """
        sid = self.config.stream_id

        q  = Gst.ElementFactory.make("queue",     None)
        wb = Gst.ElementFactory.make("webrtcbin", None)
        if not q or not wb:
            logger.error(f"[{sid}] Could not create queue/webrtcbin elements")
            return False

        wb.set_property("bundle-policy", "max-bundle")
        if _STUN_SERVER:
            wb.set_property("stun-server", _STUN_SERVER)
        turn = _turn_uri(peer_id)
        if turn:
            wb.emit("add-turn-server", turn)
            logger.info(f"[{sid}][{peer_id}] TURN configured: {_TURN_HOST}")

        offer_sent  = [False]
        tee_src_ref = [None]

        # ── Offer callback ────────────────────────────────────────────────────
        def _on_offer(promise):
            """Called by GStreamer (GLib thread) when create-offer completes."""
            promise.wait()
            reply = promise.get_reply()
            if not reply:
                logger.error(f"[{sid}][{peer_id}] create-offer: no reply")
                return
            offer = reply.get_value("offer")
            if not offer:
                err = reply.get_value("error")
                logger.error(
                    f"[{sid}][{peer_id}] create-offer: offer=None err={err}"
                )
                return
            raw_sdp   = offer.sdp.as_text()
            fixed_sdp = _fix_sdp(raw_sdp)

            # set-local-description MUST use the same SDP we send to the browser.
            # Using the original (unfixed) offer causes a mismatch: Chrome builds
            # its answer from the fixed SDP, GStreamer expects the original, and
            # rejects the answer.
            _, fixed_msg = GstSdp.SDPMessage.new_from_text(fixed_sdp)
            fixed_offer  = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.OFFER, fixed_msg)
            ld_promise = Gst.Promise.new()
            wb.emit("set-local-description", fixed_offer, ld_promise)
            ld_promise.interrupt()

            logger.info(f"[{sid}][{peer_id}] SDP offer → browser")
            send_fn({"type": "offer", "sdp": fixed_sdp})

        def _on_negotiation_needed(element):
            if offer_sent[0]:
                return
            offer_sent[0] = True
            logger.info(f"[{sid}][{peer_id}] on-negotiation-needed → create-offer")
            # Use a lambda wrapper: GI may call the change-func as (promise,) or
            # (promise, user_data) depending on version.  The lambda absorbs any
            # extra positional args so we never get a TypeError.
            promise = Gst.Promise.new_with_change_func(
                lambda p, *_: _on_offer(p), None, None)
            element.emit("create-offer", None, promise)

        # Connect signals before syncing state so on-negotiation-needed is caught.
        wb.connect("on-negotiation-needed", _on_negotiation_needed)
        wb.connect(
            "on-ice-candidate",
            lambda e, idx, cand: send_fn(
                {"type": "ice", "candidate": cand, "sdpMLineIndex": idx}
            ),
        )

        # ── Pipeline linkage — runs on GLib main loop thread ──────────────────
        def _do_link():
            try:
                self.pipeline.add(q)
                self.pipeline.add(wb)
                q.get_static_pad("src").link(wb.request_pad_simple("sink_%u"))
                tee_src = self.tee.request_pad_simple("src_%u")
                tee_src.link(q.get_static_pad("sink"))
                tee_src_ref[0] = tee_src
                q.sync_state_with_parent()
                wb.sync_state_with_parent()
                logger.info(f"[{sid}] Peer {peer_id} attached")
            except Exception as exc:
                logger.error(
                    f"[{sid}] _do_link error for peer {peer_id}: {exc}",
                    exc_info=True,
                )
            return False  # do not repeat

        GLib.idle_add(_do_link)

        # ── Cleanup ───────────────────────────────────────────────────────────
        def cleanup():
            def _do():
                try:
                    wb.set_state(Gst.State.NULL)
                    q.set_state(Gst.State.NULL)
                    self.pipeline.remove(wb)
                    self.pipeline.remove(q)
                    if tee_src_ref[0]:
                        self.tee.release_request_pad(tee_src_ref[0])
                except Exception as ex:
                    logger.warning(f"[{sid}][{peer_id}] cleanup error: {ex}")
                return False
            GLib.idle_add(_do)

        # ── Answer / ICE — called from asyncio thread, must use idle_add ──────
        def set_answer(sdp: str):
            sdp = _fix_answer(sdp)
            def _apply():
                _, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp)
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg)
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
                "wb":         wb,
                "q":          q,
                "tee_src":    None,   # filled by _do_link
                "set_answer": set_answer,
                "add_ice":    add_ice,
                "cleanup":    cleanup,
            }

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
