"""Use a real loopback WebSocket, never hardware or a production listener."""

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
import requests
import uvicorn
from websockets.sync.client import connect

from mcp_server import audio_processing, stackchan_config
from mcp_server.outbound_relay import MAX_AUDIO_REPLY, RelaySettings, create_app
from mcp_server.stackchan_client import StackchanClient, post_pcm_stream

DEVICE_TOKEN = "wire-device-0123456789abcdef"
CONTROL_TOKEN = "wire-control-0123456789abcdef"
CONTROL_HEADERS = {"Authorization": f"Bearer {CONTROL_TOKEN}"}


@pytest.fixture
def wire_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    settings = RelaySettings(DEVICE_TOKEN, CONTROL_TOKEN, command_timeout=2)
    config = uvicorn.Config(create_app(settings), ws="websockets", ws_max_queue=2,
                            ws_max_size=MAX_AUDIO_REPLY + 36, ws_per_message_deflate=False,
                            log_level="error", proxy_headers=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        assert server.started
        yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/stackchan/ws"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def test_real_websocket_pcm_snapshot_and_reconnect(wire_server):
    base, url = wire_server
    for cycle in range(2):
        with (
            connect(url, additional_headers={"Authorization": f"Bearer {DEVICE_TOKEN}"}) as ws,
            httpx.Client(base_url=base, headers=CONTROL_HEADERS, timeout=4) as http,
            ThreadPoolExecutor() as pool,
        ):
            ws.send(json.dumps({"type": "hello", "v": 1}))
            deadline = time.monotonic() + 2
            while not http.get("/relay/status").json()["online"]:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            pcm = b"\x00\x01" * 24576
            future = pool.submit(http.post, "/device/play/pcm",
                                 params={"session": f"test-{cycle}", "seq": 0, "final": 1},
                                 content=pcm, headers={"Content-Type": "audio/x-raw"})
            request = json.loads(ws.recv(timeout=2))
            assert request["path"] == "/play/pcm"
            assert ws.recv(timeout=2) == b"SCB1" + request["id"].encode() + pcm
            ws.send(json.dumps({"id": request["id"], "status": 200, "body": {"ok": True}}))
            assert future.result(timeout=3).json()["ok"]
            future = pool.submit(http.get, "/device/snapshot")
            request = json.loads(ws.recv(timeout=2))
            jpeg = b"\xff\xd8" + bytes(range(256)) * 60 + b"\xff\xd9"
            ws.send(json.dumps({"id": request["id"], "status": 200,
                                "binary_size": len(jpeg), "content_type": "image/jpeg"}))
            ws.send(b"SCB1" + request["id"].encode() + jpeg)
            response = future.result(timeout=3)
            assert response.content == jpeg
            assert response.headers["content-type"] == "image/jpeg"
            future = pool.submit(http.get, "/device/audio")
            request = json.loads(ws.recv(timeout=2))
            wav = b"RIFF" + bytes(256040)
            ws.send(json.dumps({"id": request["id"], "status": 200,
                                "binary_size": len(wav), "content_type": "audio/wav"}))
            ws.send(b"SCB1" + request["id"].encode() + wav)
            response = future.result(timeout=3)
            assert response.content == wav
            assert response.headers["content-type"] == "audio/wav"


def test_mcp_client_through_real_relay(wire_server, monkeypatch, tmp_path):
    base, url = wire_server
    monkeypatch.setattr(stackchan_config, "load_dotenv", lambda: None)
    monkeypatch.setenv("STACKCHAN_TRANSPORT", "relay")
    monkeypatch.delenv("STACKCHAN_RELAY_TOKEN_FILE", raising=False)
    config = replace(stackchan_config.load_config(), relay_url=base, relay_token=CONTROL_TOKEN,
                     pcm_first_segment_timeout=0, pcm_gain=1, pcm_limit=1, save_pcm=False,
                     pcm_declick_samples=0, pcm_zero_cross_window=0, pcm_segment_bytes=48 * 1024)
    client = StackchanClient(config)
    with (
        connect(url, additional_headers={"Authorization": f"Bearer {DEVICE_TOKEN}"}) as ws,
        ThreadPoolExecutor() as pool,
    ):
        ws.send(json.dumps({"type": "hello", "v": 1}))
        deadline = time.monotonic() + 2
        while not client.relay_status()["online"]:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        for call, expected in [
            (lambda: client.set_face("happy"), "/face"),
            (lambda: client.gesture("nod"), "/nod"),
            (lambda: client.move(5, -5, 500), "/move"),
            (client.read_env, "/env"),
            (client.playback_status, "/playback/status"),
        ]:
            future = pool.submit(call)
            request = json.loads(ws.recv(timeout=2))
            assert request["path"] == expected
            ws.send(json.dumps({"id": request["id"], "status": 200, "body": {"success": True}}))
            assert future.result(timeout=3)["success"]
        pcm = b"\x00\x00" * 30000
        future = pool.submit(post_pcm_stream, client, iter([pcm]), tmp_path, audio_processing)
        received = bytearray()
        for seq in range(2):
            request = json.loads(ws.recv(timeout=2))
            assert request["query"]["seq"] == str(seq)
            assert request["query"]["final"] == str(seq)
            payload = ws.recv(timeout=2)
            assert isinstance(payload, bytes)
            assert payload[:36] == b"SCB1" + request["id"].encode()
            received.extend(payload[36:])
            ws.send(json.dumps({"id": request["id"], "status": 200,
                                "body": {"success": True, "staged": seq == 0}}))
        assert future.result(timeout=3)["success"]
        assert bytes(received) == pcm


def test_speech_retries_only_unadmitted_segment_after_status_poll(wire_server, monkeypatch, tmp_path):
    base, url = wire_server
    monkeypatch.setattr(stackchan_config, "load_dotenv", lambda: None)
    monkeypatch.delenv("STACKCHAN_RELAY_TOKEN_FILE", raising=False)
    config = replace(stackchan_config.load_config(), transport="relay", relay_url=base,
                     relay_token=CONTROL_TOKEN, pcm_first_segment_timeout=0,
                     pcm_gain=1, pcm_limit=1, save_pcm=False,
                     pcm_declick_samples=0, pcm_zero_cross_window=0)
    client = StackchanClient(config)
    request_once = client.request
    rejected = threading.Event()
    speech_attempts = []

    def observe_request(method, request_url, **kwargs):
        if method == "post":
            speech_attempts.append((request_url, kwargs["data"]))
        try:
            return request_once(method, request_url, **kwargs)
        except requests.HTTPError as exc:
            assert exc.response is not None
            assert exc.response.status_code == 409
            assert exc.response.json() == {"error": "Device channel busy; command was not queued"}
            rejected.set()
            raise

    monkeypatch.setattr(client, "request", observe_request)
    with (
        connect(url, additional_headers={"Authorization": f"Bearer {DEVICE_TOKEN}"}) as ws,
        ThreadPoolExecutor() as pool,
    ):
        ws.send(json.dumps({"type": "hello", "v": 1}))
        deadline = time.monotonic() + 2
        while not client.relay_status()["online"]:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        poll = pool.submit(client.audio_status)
        poll_request = json.loads(ws.recv(timeout=2))
        assert poll_request["path"] == "/audio/status"
        pcm = b"\x01\x00" * 100
        speech = pool.submit(post_pcm_stream, client, iter([pcm]), tmp_path, audio_processing)
        assert rejected.wait(timeout=1)
        with pytest.raises(TimeoutError):
            ws.recv(timeout=0.05)
        ws.send(json.dumps({"id": poll_request["id"], "status": 200,
                            "body": {"ready": False}}))
        assert poll.result(timeout=1) == {"ready": False}
        accepted = json.loads(ws.recv(timeout=3))
        assert accepted["path"] == "/play/pcm"
        assert accepted["query"]["seq"] == "0"
        assert accepted["query"]["final"] == "1"
        assert ws.recv(timeout=1) == b"SCB1" + accepted["id"].encode() + pcm
        ws.send(json.dumps({"id": accepted["id"], "status": 200,
                            "body": {"success": True, "staged": False}}))
        assert speech.result(timeout=1)["success"]
        assert len(speech_attempts) == 2
        assert speech_attempts[0] == speech_attempts[1]
        with pytest.raises(TimeoutError):
            ws.recv(timeout=0.05)
