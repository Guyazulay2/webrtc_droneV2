#!/usr/bin/env python3
import asyncio, json, logging, threading, sys, faulthandler
faulthandler.enable()
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("test")

try:
    import websockets
except ImportError:
    print("\nERROR: pip install websockets\n")
    sys.exit(1)

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

Gst.init(None)

HTML = b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>WebRTC Test</title>
    <style>
        body{background:#111;color:#eee;font-family:monospace;padding:20px}
        video{width:640px;height:360px;background:#000;display:block;border:2px solid #444}
        #log{margin-top:10px;height:200px;overflow-y:auto;font-size:12px;background:#1a1a1a;padding:8px;border:1px solid #333}
        button{margin:10px 5px 0 0;padding:8px 16px;background:#4f7ef8;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px}
    </style>
</head>
<body>
    <h2>WebRTC videotestsrc</h2>
    <video id="v" autoplay muted playsinline></video>
    <div>
        <button onclick="start()">Connect</button>
    </div>
    <div id="log"></div>
    <script>
        const WS = "ws://" + location.hostname + ":8766";
        let pc, ws;
        function log(msg, color) {
            const d = document.getElementById("log");
            const line = document.createElement("div");
            line.style.color = color || "#ccc";
            line.textContent = new Date().toISOString().substr(11,8) + " " + msg;
            d.appendChild(line);
            d.scrollTop = d.scrollHeight;
        }
        async function start() {
            if (pc) pc.close();
            log("Creating PC...", "#4f7ef8");
            pc = new RTCPeerConnection();
            pc.addTransceiver("video", { direction: "recvonly" });
            pc.ontrack = e => {
                log("Track received!", "#22c55e");
                document.getElementById("v").srcObject = e.streams[0];
            };
            pc.onicecandidate = e => {
                if (e.candidate) ws.send(JSON.stringify({type:"ice", candidate:e.candidate.candidate, sdpMLineIndex:e.candidate.sdpMLineIndex}));
            };
            ws = new WebSocket(WS);
            ws.onopen = () => ws.send(JSON.stringify({type:"join"}));
            ws.onmessage = async ev => {
                const msg = JSON.parse(ev.data);
                if (msg.type === "offer") {
                    await pc.setRemoteDescription({type:"offer", sdp:msg.sdp});
                    const answer = await pc.createAnswer();
                    await pc.setLocalDescription(answer);
                    ws.send(JSON.stringify({type:"answer", sdp:answer.sdp}));
                } else if (msg.type === "ice") {
                    await pc.addIceCandidate({candidate:msg.candidate, sdpMLineIndex:msg.sdpMLineIndex});
                }
            };
        }
    </script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)
    def log_message(self, *a): pass


class Pipeline:
    def __init__(self):
        self.pipeline = None
        self.tee = None
        self._mainloop = GLib.MainLoop()
        threading.Thread(target=self._mainloop.run, daemon=True).start()

    def start(self):
        pipe_str = (
            "videotestsrc pattern=18 is-live=true "
            "! video/x-raw,width=640,height=360,framerate=30/1 "
            "! videoconvert ! x264enc tune=zerolatency bitrate=500 "
            "! rtph264pay config-interval=-1 pt=96 "
            "! tee name=t allow-not-linked=true "
            "t. ! queue ! fakesink sync=false"
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        self.tee = self.pipeline.get_by_name("t")
        self.pipeline.set_state(Gst.State.PLAYING)

    def attach_peer(self, send_fn):
        q = Gst.ElementFactory.make("queue", None)
        wb = Gst.ElementFactory.make("webrtcbin", None)
        wb.set_property("stun-server", "stun://stun.l.google.com:19302")
        wb.set_property("bundle-policy", "max-bundle")

        # SENDONLY: server sends video, browser receives
        caps = Gst.caps_from_string(
            "application/x-rtp,media=video,encoding-name=H264,payload=96"
        )
        wb.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY, caps)

        self.pipeline.add(q)
        self.pipeline.add(wb)
        q.get_static_pad("src").link(wb.request_pad_simple("sink_%u"))
        self.tee.request_pad_simple("src_%u").link(q.get_static_pad("sink"))
        q.sync_state_with_parent()
        wb.sync_state_with_parent()

        def _on_offer(promise, element, _):
            promise.wait()
            reply = promise.get_reply()
            offer = reply.get_value("offer")

            # Read SDP text while we still own the reference
            sdp_text = offer.sdp.as_text()

            # Transfer ownership to webrtcbin via set-local-description
            set_ld_promise = Gst.Promise.new()
            element.emit("set-local-description", offer, set_ld_promise)
            set_ld_promise.interrupt()  # We don't need to wait on it

            send_fn({"type": "offer", "sdp": sdp_text})

        def _on_negotiation_needed(element):
            create_promise = Gst.Promise.new_with_change_func(_on_offer, element, None)
            element.emit("create-offer", None, create_promise)

        wb.connect("on-negotiation-needed", _on_negotiation_needed)
        wb.connect(
            "on-ice-candidate",
            lambda e, i, c: send_fn({"type": "ice", "candidate": c, "sdpMLineIndex": i})
        )

        def set_answer(sdp):
            _, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp)
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg
            )
            wb.emit("set-remote-description", answer, Gst.Promise.new())

        def add_ice(candidate, sdp_mline_index):
            wb.emit("add-ice-candidate", sdp_mline_index, candidate)

        def cleanup():
            wb.set_state(Gst.State.NULL)
            q.set_state(Gst.State.NULL)
            self.pipeline.remove(wb)
            self.pipeline.remove(q)

        return set_answer, add_ice, cleanup


gst = Pipeline()

async def ws_handler(ws):
    loop = asyncio.get_running_loop()
    send_fn = lambda m: asyncio.run_coroutine_threadsafe(ws.send(json.dumps(m)), loop)
    h_ans, h_ice, cleanup = (None, None, None)
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "join":
                h_ans, h_ice, cleanup = gst.attach_peer(send_fn)
            elif msg["type"] == "answer":
                h_ans(msg["sdp"])
            elif msg["type"] == "ice":
                h_ice(msg["candidate"], msg["sdpMLineIndex"])
    finally:
        if cleanup:
            cleanup()

async def main():
    gst.start()
    threading.Thread(
        target=HTTPServer(("0.0.0.0", 8767), Handler).serve_forever,
        daemon=True
    ).start()
    async with websockets.serve(ws_handler, "0.0.0.0", 8766):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())