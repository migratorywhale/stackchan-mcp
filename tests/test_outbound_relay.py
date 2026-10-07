import asyncio
import json
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


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("status", [200, 503])
def test_booth_allowlist_preserves_method_body_and_device_status(client, enabled, status):
    method = "GET" if enabled is None else "POST"
    body = {} if enabled is None else {"enabled": enabled}
    result_body = {
        "success": status == 200,
        "booth_mode": enabled is not False,
        "persisted": status == 200,
        "capture_allowed": enabled is False,
        "mic_running": False,
    }
    if status != 200:
        result_body["error"] = "transition incomplete"
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(client.request, method, "/device/booth", headers=HEADERS,
                                 **({"json": body} if enabled is not None else {}))
            command = ws.receive_json()
            assert command["method"] == method
            assert command["path"] == "/booth"
            assert command["body"] == body
            assert command["query"] == {}
            ws.send_json({"id": command["id"], "status": status, "body": result_body})
            result = future.result(timeout=2)
            assert result.status_code == status
            assert result.json() == result_body
        assert not client.get("/relay/status", headers=HEADERS).json()["busy"]


def test_booth_allowlist_keeps_auth_and_exact_paths(client):
    for method in ("GET", "POST"):
        assert client.request(method, "/device/booth").status_code == 401
        assert client.request(method, "/device/booth", headers=DEVICE_HEADERS).status_code == 401
        assert client.request(method, "/device/booth", headers=HEADERS).status_code == 503
        assert client.request(method, "/device/booth/toggle", headers=HEADERS).status_code == 404


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


def wait_for_booth_reservation(client):
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        status = client.get("/relay/status", headers=HEADERS).json()
        if status["booth_priority_reserved"]:
            return status
        time.sleep(0.001)
    pytest.fail("Booth priority was not reserved")


@pytest.mark.parametrize("path", ["/audio/status", "/audio"])
def test_booth_enable_waits_for_complete_reply_then_precedes_new_polling(client, path):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            first = pool.submit(client.get, "/device" + path, headers=HEADERS)
            active = ws.receive_json()
            booth = pool.submit(client.post, "/device/booth", headers=HEADERS, json={"enabled": True})
            status = wait_for_booth_reservation(client)
            assert status["pending_path"] == path
            assert status["busy"]
            for _ in range(3):
                assert client.get("/device/audio/status", headers=HEADERS).status_code == 409
            assert client.post("/device/booth", headers=HEADERS, json={"enabled": True}).status_code == 409
            assert client.post("/device/nod", headers=HEADERS).status_code == 409
            assert not booth.done()
            if path == "/audio":
                wav = b"RIFF" + b"\x00" * 256040
                ws.send_json({"id": active["id"], "status": 200,
                              "binary_size": len(wav), "content_type": "audio/wav"})
                assert not booth.done()
                assert client.get("/relay/status", headers=HEADERS).json()["pending_path"] == "/audio"
                ws.send_bytes(b"SCB1" + active["id"].encode() + wav)
                assert first.result(timeout=1).content == wav
            else:
                ws.send_json({"id": active["id"], "status": 200, "body": {"ready": False}})
                assert first.result(timeout=1).status_code == 200
            command = ws.receive_json()
            assert command["path"] == "/booth"
            assert command["body"] == {"enabled": True}
            assert command["id"] != active["id"]
            ws.send_json({"id": command["id"], "status": 200, "body": {"success": True}})
            assert booth.result(timeout=1).status_code == 200
        status = client.get("/relay/status", headers=HEADERS).json()
        assert not status["busy"]
        assert not status["booth_priority_reserved"]


@pytest.mark.parametrize("body", [{"enabled": False}, {"enabled": 1}, {"enabled": "true"},
                                  {"enabled": True, "extra": 1}, {}])
def test_only_exact_enable_receives_priority(client, body):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            first = pool.submit(client.get, "/device/audio/status", headers=HEADERS)
            active = ws.receive_json()
            assert client.post("/device/booth", headers=HEADERS, json=body).status_code == 409
            status = client.get("/relay/status", headers=HEADERS).json()
            assert not status["booth_priority_reserved"]
            assert status["busy"] and status["pending_path"] == "/audio/status"
            ws.send_json({"id": active["id"], "status": 200, "body": {"ready": False}})
            assert first.result(timeout=1).status_code == 200


@pytest.mark.parametrize("body", [{"enabled": False}, {"enabled": 1}, {"enabled": "true"},
                                  {"enabled": True, "extra": 1}, {}])
def test_nonpriority_booth_arriving_busy_cannot_dispatch_after_slow_upload(body):
    async def run():
        relay = Relay(RelaySettings(DEVICE, CONTROL))
        peer = Peer(None, ready=True)
        relay.peer = peer
        relay.admitted = peer

        async def receive():
            await asyncio.sleep(0)
            relay.admitted = None
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

        scope = {"type": "http", "method": "POST", "path_params": {"path": "booth"},
                 "query_string": b"",
                 "headers": [(b"authorization", f"Bearer {CONTROL}".encode())]}
        response = await relay.command(Request(scope, receive=receive))
        assert response.status_code == 409
        assert peer.pending is None
        assert relay.admitted is None
        assert relay.booth_reservation is None
    asyncio.run(run())


def test_booth_priority_finishes_active_pcm_frame_but_rejects_later_segments(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as ws:
        ready(client, ws)
        with ThreadPoolExecutor() as pool:
            pcm = b"\x00\x01" * 2400
            first = pool.submit(client.post, "/device/play/pcm?session=test&seq=0&final=0",
                                headers={**HEADERS, "Content-Type": "audio/x-raw"}, content=pcm)
            active = ws.receive_json()
            assert ws.receive_bytes() == b"SCB1" + active["id"].encode() + pcm
            booth = pool.submit(client.post, "/device/booth", headers=HEADERS, json={"enabled": True})
            assert wait_for_booth_reservation(client)["pending_path"] == "/play/pcm"
            ws.send_json({"id": active["id"], "status": 200, "body": {"queued": True}})
            assert first.result(timeout=1).status_code == 200
            command = ws.receive_json()
            assert command["path"] == "/booth"
            next_segment = client.post("/device/play/pcm?session=test&seq=1&final=1",
                                       headers={**HEADERS, "Content-Type": "audio/x-raw"}, content=pcm)
            assert next_segment.status_code == 409
            assert "not queued" in next_segment.json()["error"]
            ws.send_json({"id": command["id"], "status": 200, "body": {"success": True}})
            assert booth.result(timeout=1).status_code == 200
        assert not client.get("/relay/status", headers=HEADERS).json()["busy"]


def test_booth_reservation_is_not_carried_to_reconnected_device(client):
    with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as old:
        ready(client, old)
        with ThreadPoolExecutor() as pool:
            first = pool.submit(client.get, "/device/audio/status", headers=HEADERS)
            old.receive_json()
            booth = pool.submit(client.post, "/device/booth", headers=HEADERS, json={"enabled": True})
            wait_for_booth_reservation(client)
            with client.websocket_connect("/stackchan/ws", headers=DEVICE_HEADERS) as new:
                ready(client, new)
                assert first.result(timeout=1).status_code == 503
                result = booth.result(timeout=1)
                assert result.status_code == 503
                assert "not sent" in result.json()["error"]
                assert not client.get("/relay/status", headers=HEADERS).json()["booth_priority_reserved"]
                next_call = pool.submit(client.get, "/device/booth", headers=HEADERS)
                command = new.receive_json()
                assert command["method"] == "GET"
                new.send_json({"id": command["id"], "status": 200, "body": {"booth_mode": False}})
                assert next_call.result(timeout=1).status_code == 200


@pytest.mark.parametrize("end", ["timeout", "cancel", "disconnect", "bad_body"])
def test_abandoned_booth_reservation_never_releases_active_owner(end):
    async def run():
        relay = Relay(RelaySettings(DEVICE, CONTROL, command_timeout=0.05))
        peer = Peer(None, ready=True)
        relay.peer = peer
        relay.admitted = peer
        read = False

        async def receive():
            nonlocal read
            if not read:
                read = True
                return {"type": "http.request", "body": b"[" if end == "bad_body" else b'{"enabled":true}',
                        "more_body": False}
            if end == "disconnect":
                return {"type": "http.disconnect"}
            await asyncio.Event().wait()

        scope = {"type": "http", "method": "POST", "path_params": {"path": "booth"},
                 "query_string": b"",
                 "headers": [(b"authorization", f"Bearer {CONTROL}".encode())]}
        task = asyncio.create_task(relay.command(Request(scope, receive=receive)))
        if end == "cancel":
            await asyncio.sleep(0)
            assert relay.booth_reservation is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            response = await task
            assert response.status_code == {"timeout": 408, "disconnect": 499, "bad_body": 400}[end]
            if end != "bad_body":
                assert "not sent" in json.loads(response.body)["error"]
        assert relay.admitted is peer
        assert relay.booth_reservation is None
        assert peer.pending is None
    asyncio.run(run())
