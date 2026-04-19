"""
Main Backend Server - Working Version
WebRTC signaling + KLV telemetry
"""
import asyncio, json, logging, os, uuid
from contextlib import asynccontextmanager
from typing import Dict, Set, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from pipeline import PipelineManager, StreamConfig

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")

pipeline_mgr:  Optional[PipelineManager] = None
telemetry_mgr = None
signaling_mgr  = None
_loop:         Optional[asyncio.AbstractEventLoop] = None


class SignalingManager:
    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}

    async def connect(self, peer_id: str, ws: WebSocket):
        await ws.accept()
        self.connections[peer_id] = ws
        logger.info(f"Signaling peer connected: {peer_id}")

    def disconnect(self, peer_id: str):
        self.connections.pop(peer_id, None)
        logger.info(f"Signaling peer disconnected: {peer_id}")

    async def send(self, peer_id: str, msg: dict):
        ws = self.connections.get(peer_id)
        if ws:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception as e:
                logger.error(f"Signaling send error: {e}")


class TelemetryManager:
    def __init__(self):
        self.subscribers: Dict[str, Set[WebSocket]] = {}

    async def subscribe(self, stream_id: str, ws: WebSocket):
        await ws.accept()
        self.subscribers.setdefault(stream_id, set()).add(ws)
        logger.info(f"Telemetry subscriber added for stream {stream_id}")

    def unsubscribe(self, stream_id: str, ws: WebSocket):
        self.subscribers.get(stream_id, set()).discard(ws)

    async def broadcast(self, stream_id: str, data: dict):
        subs = self.subscribers.get(stream_id, set())
        if not subs:
            return
        payload = json.dumps({"type": "klv", "stream_id": stream_id, "data": data})
        dead = set()
        for ws in list(subs):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        subs -= dead


def on_klv_received(stream_id, klv):
    if _loop and telemetry_mgr:
        asyncio.run_coroutine_threadsafe(
            telemetry_mgr.broadcast(stream_id, klv.to_dict()), _loop)


def send_signaling(peer_id: str, msg: dict):
    if _loop and signaling_mgr:
        asyncio.run_coroutine_threadsafe(
            signaling_mgr.send(peer_id, msg), _loop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline_mgr, telemetry_mgr, signaling_mgr, _loop
    _loop         = asyncio.get_event_loop()
    telemetry_mgr = TelemetryManager()
    signaling_mgr = SignalingManager()
    pipeline_mgr  = PipelineManager(
        on_klv=on_klv_received,
        send_signaling=send_signaling,
    )
    pipeline_mgr.start_mainloop()
    if hasattr(pipeline_mgr, "set_event_loop"):
        pipeline_mgr.set_event_loop(_loop)
    logger.info("Server started")
    for i, uri in enumerate(os.getenv("STREAMS", "").split()):
        if uri.strip():
            pipeline_mgr.add_stream(StreamConfig(
                stream_id=f"auto_{i}", uri=uri.strip(), name=f"Stream {i+1}"))
    yield
    pipeline_mgr.stop_mainloop()
    logger.info("Server stopped")


app = FastAPI(title="ISR Dashboard", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_HERE    = os.path.dirname(os.path.abspath(__file__))
_UI_DIRS = [os.path.join(_HERE, "..", "ui"), "/var/www/html", os.path.join(_HERE, "ui")]
UI_DIR   = next((os.path.realpath(d) for d in _UI_DIRS if os.path.isdir(d)), None)
if UI_DIR:
    logger.info(f"Serving UI from: {UI_DIR}")


def _file(name, mime=None):
    if UI_DIR:
        p = os.path.join(UI_DIR, name)
        if os.path.exists(p):
            return FileResponse(p, media_type=mime) if mime else FileResponse(p)
    raise HTTPException(404, f"{name} not found")


@app.get("/")
async def root():     return _file("index.html")

@app.get("/app.js")
async def appjs():    return _file("app.js",   "application/javascript")

@app.get("/style.css")
async def css():      return _file("style.css", "text/css")


class AddStreamRequest(BaseModel):
    uri:    str
    name:   str = ""
    width:  int = 1280
    height: int = 720


@app.get("/api/streams")
async def list_streams():
    return {"streams": pipeline_mgr.get_stream_list() if pipeline_mgr else []}


@app.post("/api/streams")
async def add_stream(req: AddStreamRequest):
    if not pipeline_mgr:
        raise HTTPException(503)
    sid = str(uuid.uuid4())[:8]
    cfg = StreamConfig(
        stream_id=sid, uri=req.uri,
        name=req.name or req.uri,
        width=req.width, height=req.height,
    )
    if not pipeline_mgr.add_stream(cfg):
        raise HTTPException(500, "Failed to start pipeline")
    logger.info(f"Stream added: {sid} {req.uri}")
    return {"stream_id": sid, "uri": req.uri, "name": cfg.name}


@app.delete("/api/streams/{stream_id}")
async def del_stream(stream_id: str):
    if pipeline_mgr:
        pipeline_mgr.remove_stream(stream_id)
    return {"status": "removed", "stream_id": stream_id}


@app.get("/api/health")
async def health():
    return {"status": "ok",
            "streams": len(pipeline_mgr.streams) if pipeline_mgr else 0}


@app.websocket("/ws/signaling/{stream_id}")
async def signaling_ws(ws: WebSocket, stream_id: str):
    peer_id = str(uuid.uuid4())[:12]
    await signaling_mgr.connect(peer_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t   = msg.get("type")
            if t == "join":
                logger.info(f"Peer {peer_id} joining stream {stream_id}")
                peer = pipeline_mgr.add_peer(peer_id, stream_id) if pipeline_mgr else None
                if not peer:
                    await ws.send_text(json.dumps(
                        {"type": "error", "message": f"Stream {stream_id} not found"}))
            elif t == "sdp" and msg.get("data", {}).get("type") == "answer":
                if pipeline_mgr:
                    sdp_answer = msg["data"]["sdp"]
                    logger.info(f"Answer SDP from browser:\n{sdp_answer}")
                    pipeline_mgr.handle_peer_sdp(peer_id, stream_id, sdp_answer)
            elif t == "ice":
                if pipeline_mgr:
                    ice_data = msg.get("data", {})
                    candidate = ice_data.get("candidate", "")
                    mline = ice_data.get("sdpMLineIndex", 0)
                    logger.info(f"ICE from browser: mline={mline} cand={candidate[:50] if candidate else 'EMPTY'}")
                    pipeline_mgr.handle_peer_ice(peer_id, stream_id, candidate, mline)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Signaling error: {e}")
    finally:
        signaling_mgr.disconnect(peer_id)
        if pipeline_mgr:
            pipeline_mgr.remove_peer(peer_id, stream_id)


@app.websocket("/ws/telemetry/{stream_id}")
async def telemetry_ws(ws: WebSocket, stream_id: str):
    await telemetry_mgr.subscribe(stream_id, ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        telemetry_mgr.unsubscribe(stream_id, ws)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
