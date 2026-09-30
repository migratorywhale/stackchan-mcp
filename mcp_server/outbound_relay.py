"""Single-body outbound channel. Run with ``python -m mcp_server.outbound_relay``."""

import asyncio
import contextlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

MAX_BINARY = 128 * 1024
MAX_AUDIO_REPLY = 512 * 1024
MAX_TEXT = 8192
MAGIC = b"SCB1"
HEADER_SIZE = 36
ALLOWED = {
    ("GET", "/status"), ("GET", "/env"), ("GET", "/face"),
    ("GET", "/snapshot"), ("GET", "/audio/status"),
    ("GET", "/playback/status"), ("GET", "/audio"),
    ("GET", "/booth"), ("POST", "/booth"),
    ("POST", "/mode"), ("POST", "/face"), ("POST", "/move"),
    ("POST", "/home"), ("POST", "/nod"), ("POST", "/shake"),
    ("POST", "/play/pcm"),
}


@dataclass(frozen=True)
class RelaySettings:
    device_token: str = field(repr=False)
    control_token: str = field(repr=False)
    command_timeout: float = 20.0
    heartbeat_timeout: float = 35.0
    hello_timeout: float = 5.0
    upload_timeout: float = 5.0

    def __post_init__(self) -> None:
        for token in (self.device_token, self.control_token):
            if len(token) < 24 or not token.isascii() or any(c.isspace() for c in token):
                raise ValueError("Relay tokens must be at least 24 non-whitespace ASCII characters")
        if hmac.compare_digest(self.device_token, self.control_token):
            raise ValueError("Device and control tokens must be distinct")
        if min(self.command_timeout, self.heartbeat_timeout,
               self.hello_timeout, self.upload_timeout) <= 0:
            raise ValueError("Relay timeouts must be positive")

    @classmethod
    def from_env(cls) -> "RelaySettings":
        def secret(name: str) -> str:
            file = os.environ.get(name + "_FILE")
            return Path(file).read_text().strip() if file else os.environ.get(name, "")

        return cls(
            secret("STACKCHAN_RELAY_DEVICE_TOKEN"),
            secret("STACKCHAN_RELAY_TOKEN"),
        )


class ChannelError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass
class Pending:
    id: str
    future: asyncio.Future[Response]
    path: str = ""
    binary_size: int | None = None
    status: int = 200
    content_type: str = "application/octet-stream"


@dataclass
class Peer:
    socket: WebSocket
    last_seen: float = field(default_factory=time.monotonic)
    ready: bool = False
    pending: Pending | None = None


def authorized(headers: Any, token: str) -> bool:
    value = headers.get("authorization", "")
    return hmac.compare_digest(value.encode(), ("Bearer " + token).encode())


class Relay:
    def __init__(self, settings: RelaySettings):
        self.settings = settings
        self.peer: Peer | None = None
        self.admitted: Peer | None = None

    def online(self) -> bool:
        return bool(self.peer and self.peer.ready
                    and time.monotonic() - self.peer.last_seen < self.settings.heartbeat_timeout)

    async def disconnect(self, peer: Peer, message: str) -> None:
        peer.ready = False
        if self.peer is peer:
            self.peer = None
        if peer.pending and not peer.pending.future.done():
            peer.pending.future.set_exception(ChannelError(503, message))
        with contextlib.suppress(RuntimeError, WebSocketDisconnect, OSError, TimeoutError):
            await asyncio.wait_for(peer.socket.close(code=1012), 1.0)

    async def device(self, socket: WebSocket) -> None:
        if not authorized(socket.headers, self.settings.device_token):
            await socket.close(code=1008)
            return
        await socket.accept()
        peer = Peer(socket)
        old, self.peer = self.peer, peer
        if old:
            await self.disconnect(old, "Device reconnected; command was not retried")
        try:
            while True:
                timeout = self.settings.heartbeat_timeout if peer.ready else self.settings.hello_timeout
                event = await asyncio.wait_for(socket.receive(), timeout)
                if event["type"] == "websocket.disconnect":
                    break
                peer.last_seen = time.monotonic()
                raw = event.get("bytes")
                if raw is not None:
                    self.binary_reply(peer, raw)
                    continue
                text = event.get("text", "")
                if len(text.encode()) > MAX_TEXT:
                    raise ValueError("Oversized device message")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("Invalid device message")
                if not peer.ready:
                    if data != {"type": "hello", "v": 1}:
                        raise ValueError("Expected protocol hello")
                    peer.ready = True
                elif data.get("type") == "heartbeat":
                    continue
                else:
                    self.text_reply(peer, data)
        except (WebSocketDisconnect, TimeoutError, ValueError, OSError, RuntimeError):
            pass
        finally:
            await self.disconnect(peer, "Device disconnected; command was not retried")

    @staticmethod
    def text_reply(peer: Peer, data: dict[str, Any]) -> None:
        pending = peer.pending
        if not pending or pending.future.done() or data.get("id") != pending.id:
            return
        status = data.get("status")
        if type(status) is not int or not 200 <= status <= 599:
            raise ValueError("Invalid response status")
        if "binary_size" in data:
            size = data["binary_size"]
            expected_type, limit = {
                "/snapshot": ("image/jpeg", MAX_BINARY),
                "/audio": ("audio/wav", MAX_AUDIO_REPLY),
            }.get(pending.path, (None, 0))
            if type(size) is not int or not 0 < size <= limit:
                raise ValueError("Invalid binary size")
            if data.get("content_type") != expected_type:
                raise ValueError("Unsupported binary response")
            pending.binary_size, pending.status = size, status
            pending.content_type = str(expected_type)
        else:
            body = data.get("body")
            if not isinstance(body, dict):
                raise ValueError("Expected object response")
            pending.future.set_result(JSONResponse(body, status_code=status))

    @staticmethod
    def binary_reply(peer: Peer, raw: bytes) -> None:
        if (not peer.ready or not HEADER_SIZE <= len(raw) <= MAX_AUDIO_REPLY + HEADER_SIZE
                or not raw.startswith(MAGIC)):
            raise ValueError("Invalid binary frame")
        pending = peer.pending
        if not pending or pending.future.done() or raw[4:36] != pending.id.encode():
            return
        if pending.binary_size is None or len(raw) != HEADER_SIZE + pending.binary_size:
            raise ValueError("Binary response length mismatch")
        pending.future.set_result(Response(raw[HEADER_SIZE:], status_code=pending.status,
                                           media_type=pending.content_type))

    async def status(self, request: Request) -> Response:
        if not authorized(request.headers, self.settings.control_token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        peer = self.peer
        return JSONResponse({
            "online": self.online(),
            "busy": bool(self.admitted or (peer and peer.pending)),
            "last_seen_age": round(time.monotonic() - peer.last_seen, 3) if peer else None,
        })

    async def command(self, request: Request) -> Response:
        if not authorized(request.headers, self.settings.control_token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        path = "/" + request.path_params["path"]
        if (request.method, path) not in ALLOWED:
            return JSONResponse({"error": "Command not supported by outbound channel"}, status_code=404)
        peer = self.peer
        if not peer or not self.online():
            return JSONResponse({"error": "Device outbound channel is offline"}, status_code=503)
        if self.admitted or peer.pending:
            return JSONResponse({"error": "Device channel busy; command was not queued"}, status_code=409)
        self.admitted = peer
        deadline = time.monotonic() + self.settings.command_timeout
        try:
            raw = bytearray()
            binary = path == "/play/pcm"
            limit = MAX_BINARY if binary else MAX_TEXT
            try:
                async with asyncio.timeout(min(self.settings.upload_timeout, self.settings.command_timeout)):
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > limit:
                            raise ChannelError(413, "Request body too large")
                        raw.extend(chunk)
            except TimeoutError as exc:
                raise ChannelError(408, "Upload timed out; command was not sent") from exc
            body: dict[str, Any] = {}
            if binary:
                if not raw or len(raw) % 2:
                    raise ChannelError(400, "Expected nonempty s16le PCM")
                if not request.headers.get("content-type", "").startswith("audio/x-raw"):
                    raise ChannelError(415, "Expected audio/x-raw PCM")
            elif raw:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ChannelError(400, "Expected JSON object")
            query = dict(request.query_params)
            if len(str(query)) > 1024:
                raise ChannelError(400, "Query too large")
            return await self.dispatch(peer, deadline, request.method, path, query, body,
                                       bytes(raw) if binary else b"")
        except (ValueError, UnicodeError):
            return JSONResponse({"error": "Invalid request body"}, status_code=400)
        except ChannelError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        finally:
            self.admitted = None

    async def dispatch(self, peer: Peer, deadline: float, method: str, path: str, query: dict[str, str],
                       body: dict[str, Any], binary: bytes) -> Response:
        if self.peer is not peer or not self.online():
            raise ChannelError(503, "Device changed during upload; command was not sent")
        if time.monotonic() >= deadline:
            raise ChannelError(408, "Request deadline elapsed; command was not sent")
        if peer.pending:
            raise ChannelError(409, "Device channel busy; command was not queued")
        pending = Pending(uuid.uuid4().hex, asyncio.get_running_loop().create_future(), path=path)
        peer.pending = pending
        message: dict[str, Any] = {
            "v": 1, "id": pending.id, "method": method, "path": path,
            "query": query, "body": body,
            "expires_at_ms": int((time.time() + deadline - time.monotonic()) * 1000),
        }
        if binary:
            message["binary_size"] = len(binary)
        try:
            if len(json.dumps(message).encode()) > MAX_TEXT:
                raise ChannelError(413, "Command envelope too large")
            # No queue or automatic resend: a timed-out actuation has unknown completion.
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                await peer.socket.send_json(message)
                if binary:
                    await peer.socket.send_bytes(MAGIC + pending.id.encode() + binary)
                return await pending.future
        except TimeoutError as exc:
            await self.disconnect(peer, "Command timed out")
            raise ChannelError(504, "Command timed out; completion unknown, not retried") from exc
        except (OSError, WebSocketDisconnect, RuntimeError) as exc:
            await self.disconnect(peer, "Channel write failed")
            raise ChannelError(503, "Channel write failed; completion unknown, not retried") from exc
        except asyncio.CancelledError:
            await self.disconnect(peer, "Command caller cancelled")
            raise
        finally:
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()
            if peer.pending is pending:
                peer.pending = None


def create_app(settings: RelaySettings) -> Starlette:
    relay = Relay(settings)
    app = Starlette(routes=[
        WebSocketRoute("/stackchan/ws", relay.device),
        Route("/relay/status", relay.status),
        Route("/device/{path:path}", relay.command, methods=["GET", "POST"]),
    ])
    app.state.relay = relay
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(RelaySettings.from_env()), host="127.0.0.1", port=8766,
                workers=1, ws="websockets", ws_max_size=MAX_AUDIO_REPLY + HEADER_SIZE,
                ws_max_queue=2, ws_per_message_deflate=False, access_log=False,
                proxy_headers=False)


if __name__ == "__main__":
    main()
