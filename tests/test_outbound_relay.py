import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mcp_server.outbound_relay import (
    MAX_AUDIO_REPLY,
    MAX_BINARY,
    Peer,
    Relay,
    RelaySettings,
    create_app,
)

DEVICE = "device-test-secret-0123456789"
CONTROL = "control-test-secret-9876543210"
DEVICE_HEADERS = {"Authorization": f"Bearer {DEVICE}"}
HEADERS = {"Authorization": f"Bearer {CONTROL}"}


@pytest.fixture
def client():
    with TestClient(create_app(RelaySettings(DEVICE, CONTROL, command_timeout=1))) as instance:
        yield instance


def ready(client, ws):
    ws.send_json({"type": "hello", "v": 1})
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if client.get("/relay/status", headers=HEADERS).json()["online"]:
            return
        time.sleep(0.001)
    pytest.fail("Device did not become ready")


def test_distinct_credentials_required():
    assert DEVICE not in repr(RelaySettings(DEVICE, CONTROL))
    assert CONTROL not in repr(RelaySettings(DEVICE, CONTROL))
    with pytest.raises(ValueError):
        RelaySettings(DEVICE, DEVICE)
    with pytest.raises(ValueError):
        RelaySettings("", CONTROL)


def test_auth_roles_and_offline(client):
    assert client.get("/relay/status").status_code == 401
    assert client.get("/relay/status", headers=DEVICE_HEADERS).status_code == 401
    assert client.post("/device/nod").status_code == 401
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/stackchan/ws", headers=HEADERS
    ):
        pass
    assert not client.get("/relay/status", headers=HEADERS).json()["online"]
    assert client.post("/device/nod", headers=HEADERS).status_code == 503
    assert client.get("/device/audio", headers=HEADERS).status_code == 503
    assert client.post("/device/play", headers=HEADERS).status_code == 404


def test_command_and_busy_are_not_queued(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.post, "/device/face", headers=HEADERS,
                                   json={"name": "happy"})
            command = ws.receive_json()
            assert command["path"] == "/face"
            assert command["body"] == {"name": "happy"}
            assert command["v"] == 1
            assert len(command["id"]) == 32
            assert time.time() * 1000 < command["expires_at_ms"] <= (time.time() + 1) * 1000
            assert client.post("/device/nod", headers=HEADERS).status_code == 409
            ws.send_json({"type": "heartbeat"})
            ws.send_json({"id": "0" * 32, "status": 200, "body": {"wrong": True}})
            ws.send_json({"id": command["id"], "status": 200, "body": {"ok": True}})
            result = response.result(timeout=2)
            assert result.status_code == 200
            assert result.json() == {"ok": True}
        assert not client.get("/relay/status", headers=HEADERS).json()["busy"]


def test_pcm_and_jpeg_round_trip(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            pcm = b"\x00\x01" * 24000
            response = pool.submit(client.post, "/device/play/pcm?session=test&seq=0&final=1",
                                   headers={**HEADERS, "Content-Type": "audio/x-raw"}, content=pcm)
            command = ws.receive_json()
            assert command["query"] == {"session": "test", "seq": "0", "final": "1"}
            assert command["binary_size"] == len(pcm)
            assert ws.receive_bytes() == b"SCB1" + command["id"].encode() + pcm
            ws.send_json({"id": command["id"], "status": 200, "body": {"queued": True}})
            assert response.result(timeout=2).status_code == 200
            response = pool.submit(client.get, "/device/snapshot", headers=HEADERS)
            command = ws.receive_json()
            jpeg = b"\xff\xd8test-image\xff\xd9"
            ws.send_json({"id": command["id"], "status": 200,
                          "binary_size": len(jpeg), "content_type": "image/jpeg"})
            ws.send_bytes(b"SCB1" + command["id"].encode() + jpeg)
            result = response.result(timeout=2)
            assert result.headers["content-type"] == "image/jpeg"
            assert result.content == jpeg


@pytest.mark.parametrize("path,content_type,size,accepted", [
    ("/audio", "audio/wav", 256044, True),
    ("/audio", "audio/wav", MAX_AUDIO_REPLY + 1, False),
    ("/snapshot", "image/jpeg", MAX_BINARY + 1, False),
    ("/snapshot", "audio/wav", 100, False),
    ("/env", "image/jpeg", 100, False),
])
def test_binary_response_route_and_size(client, path, content_type, size, accepted):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(client.get, "/device" + path, headers=HEADERS)
            command = ws.receive_json()
            ws.send_json({"id": command["id"], "status": 200,
                          "binary_size": size, "content_type": content_type})
            if accepted:
                data = b"RIFF" + b"\x00" * (size - 4)
                ws.send_bytes(b"SCB1" + command["id"].encode() + data)
                response = future.result(timeout=2)
                assert response.content == data
                assert response.headers["content-type"] == content_type
            else:
                assert future.result(timeout=2).status_code == 503


def test_timeout_drops_connection_and_never_replays(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.post, "/device/nod", headers=HEADERS)
            ws.receive_json()
            assert response.result(timeout=3).status_code == 504
        assert not client.get("/relay/status", headers=HEADERS).json()["online"]
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.post, "/device/home", headers=HEADERS)
            command = ws.receive_json()
            assert command["path"] == "/home"
            ws.send_json({"id": command["id"], "status": 200, "body": {"ok": True}})
            assert response.result(timeout=2).status_code == 200


def test_reconnect_fails_old_request_without_affecting_new_peer(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as old:
        ready(client, old)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, "/device/env", headers=HEADERS)
            old.receive_json()
            with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as new:
                ready(client, new)
                assert response.result(timeout=2).status_code == 503
                assert client.get("/relay/status", headers=HEADERS).json()["online"]


def test_disconnect_fails_pending(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, "/device/status", headers=HEADERS)
            ws.receive_json()
            ws.close()
            assert response.result(timeout=2).status_code == 503


@pytest.mark.parametrize("body,content_type,code", [
    (b"a", "audio/x-raw", 400),
    (b"", "audio/x-raw", 400),
    (b"aa", "text/plain", 415),
    (b"x" * (MAX_BINARY + 2), "audio/x-raw", 413),
])
def test_pcm_input_limits(client, body, content_type, code):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        assert client.post("/device/play/pcm", headers={**HEADERS, "Content-Type": content_type},
                           content=body).status_code == code


def test_bad_binary_reply_disconnects(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            response = pool.submit(client.get, "/device/snapshot", headers=HEADERS)
            command = ws.receive_json()
            ws.send_json({"id": command["id"], "status": 200,
                          "binary_size": 10, "content_type": "image/jpeg"})
            ws.send_bytes(b"SCB1" + command["id"].encode() + b"short")
            assert response.result(timeout=2).status_code == 503


def test_missing_heartbeat_closes_channel():
    settings = RelaySettings(DEVICE, CONTROL, heartbeat_timeout=0.1)
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws,
    ):
        ready(client, ws)
        time.sleep(0.15)
        assert not client.get("/relay/status", headers=HEADERS).json()["online"]


def test_no_hello_cannot_run_commands(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS):
        assert client.get("/device/env", headers=HEADERS).status_code == 503


def test_stalled_upload_reserves_bounded_admission_and_times_out():
    async def run():
        relay = Relay(RelaySettings(DEVICE, CONTROL, upload_timeout=0.03))
        relay.peer = Peer(None, ready=True)
        entered = asyncio.Event()

        async def stalled_receive():
            entered.set()
            await asyncio.Event().wait()

        scope = {"type": "http", "method": "POST", "path_params": {"path": "face"},
                 "headers": [(b"authorization", f"Bearer {CONTROL}".encode())]}
        first = asyncio.create_task(relay.command(Request(scope, receive=stalled_receive)))
        await entered.wait()
        assert relay.admitted is relay.peer
        second = await relay.command(Request(scope))
        assert second.status_code == 409
        assert (await first).status_code == 408
        assert relay.admitted is None
        assert relay.peer.pending is None
    asyncio.run(run())


def test_connection_change_while_uploading_does_not_dispatch_on_new_peer():
    async def run():
        relay = Relay(RelaySettings(DEVICE, CONTROL))
        relay.peer = Peer(None, ready=True)

        async def receive():
            relay.peer = Peer(None, ready=True)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {"type": "http", "method": "POST", "path_params": {"path": "face"},
                 "query_string": b"",
                 "headers": [(b"authorization", f"Bearer {CONTROL}".encode())]}
        response = await relay.command(Request(scope, receive=receive))
        assert response.status_code == 503
        assert relay.peer.pending is None
        assert relay.admitted is None
    asyncio.run(run())
