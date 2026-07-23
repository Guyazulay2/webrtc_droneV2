"""
Redis pub/sub bridge for multi-pod UDP routing.

The NLB sends a UDP stream to exactly ONE pod.  That pod ingests and
publishes to Redis.  ALL pods (including the ingesting pod) subscribe
and feed their own GStreamer / WebSocket pipelines — so every pod can
serve any browser regardless of which pod received the UDP.

Channels:
  stream:rtp:<stream_id>  → raw RTP video bytes (one message per packet)
  stream:klv:<stream_id>  → JSON-encoded KLV dict (one message per frame)
"""

import asyncio
import logging
from typing import Optional, Callable

logger = logging.getLogger("redis_bridge")

_RTP_PFX = "stream:rtp:"
_KLV_PFX = "stream:klv:"


class UDPIngestor:
    """
    Binds to a UDP port via asyncio datagram socket.
    Publishes every raw RTP packet to Redis.
    Runs on every pod; only the one the NLB routes to will actually receive.
    """

    def __init__(self, port: int, redis, stream_id: str):
        self._port      = port
        self._redis     = redis
        self._channel   = f"{_RTP_PFX}{stream_id}"
        self._transport = None

    async def start(self):
        loop    = asyncio.get_event_loop()
        redis   = self._redis
        channel = self._channel
        port    = self._port

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                asyncio.ensure_future(redis.publish(channel, data))
            def error_received(self, exc):
                logger.warning(f"UDP/{port}: {exc}")

        self._transport, _ = await loop.create_datagram_endpoint(
            _Proto, local_addr=("0.0.0.0", port))
        logger.info(f"UDPIngestor :{port} → [{self._channel}]")

    def stop(self):
        if self._transport:
            self._transport.close()
            self._transport = None


class KLVIngestor:
    """
    Binds to a UDP port for raw KLV bytes.
    Parses each datagram and publishes JSON to Redis.
    """

    def __init__(self, port: int, redis, stream_id: str, parser_fn: Callable):
        self._port      = port
        self._redis     = redis
        self._channel   = f"{_KLV_PFX}{stream_id}"
        self._parser    = parser_fn
        self._transport = None

    async def start(self):
        import json
        loop    = asyncio.get_event_loop()
        redis   = self._redis
        channel = self._channel
        parser  = self._parser
        port    = self._port

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    klv = parser(data)
                    if klv is None:
                        return
                    payload = klv.to_dict() if hasattr(klv, "to_dict") else klv
                    asyncio.ensure_future(redis.publish(channel, json.dumps(payload)))
                except Exception:
                    pass
            def error_received(self, exc):
                logger.warning(f"KLV UDP/{port}: {exc}")

        self._transport, _ = await loop.create_datagram_endpoint(
            _Proto, local_addr=("0.0.0.0", port))
        logger.info(f"KLVIngestor :{port} → [{self._channel}]")

    def stop(self):
        if self._transport:
            self._transport.close()
            self._transport = None


class RedisVideoFeeder:
    """
    Subscribes to a Redis RTP channel and pushes each packet into GStreamer appsrc.
    Runs on every pod so all pods can serve WebRTC peers for any stream.
    """

    def __init__(self, stream_id: str, redis, appsrc):
        self._channel = f"{_RTP_PFX}{stream_id}"
        self._redis   = redis
        self._appsrc  = appsrc
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(
            self._run(), name=f"vid-feeder:{self._channel}")

    async def _run(self):
        from gi.repository import Gst, GLib
        async with self._redis.pubsub() as ps:
            await ps.subscribe(self._channel)
            logger.info(f"RedisVideoFeeder subscribed [{self._channel}]")
            async for msg in ps.listen():
                if msg["type"] != "message":
                    continue
                data = msg["data"]
                if not isinstance(data, (bytes, bytearray)):
                    continue
                buf    = Gst.Buffer.new_wrapped(bytes(data))
                appsrc = self._appsrc
                # Push on the GLib main loop thread for safe pipeline interaction.
                GLib.idle_add(lambda b=buf: appsrc.emit("push-buffer", b) and False)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()


class KLVRedisSubscriber:
    """
    Subscribes to a Redis KLV channel.
    Calls on_klv(stream_id, klv_dict) for each message — each pod
    broadcasts to its own WebSocket subscribers.
    """

    def __init__(self, stream_id: str, redis, on_klv: Callable):
        self._stream_id = stream_id
        self._channel   = f"{_KLV_PFX}{stream_id}"
        self._redis     = redis
        self._on_klv    = on_klv
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(
            self._run(), name=f"klv-sub:{self._channel}")

    async def _run(self):
        import json
        async with self._redis.pubsub() as ps:
            await ps.subscribe(self._channel)
            logger.info(f"KLVRedisSubscriber subscribed [{self._channel}]")
            async for msg in ps.listen():
                if msg["type"] != "message":
                    continue
                try:
                    raw = msg["data"]
                    if isinstance(raw, bytes):
                        raw = raw.decode()
                    await self._on_klv(self._stream_id, json.loads(raw))
                except Exception as e:
                    logger.warning(f"KLV Redis sub error: {e}")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
