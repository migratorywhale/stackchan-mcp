import json
import os
import wave
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from mcp_server import audio_processing, mcp_tools, stackchan_config
from mcp_server.stackchan_client import (
    PcmPlaybackError,
    StackchanClient,
    post_pcm_stream,
    post_pcm_tcp_stream,
    post_pcm_udp_stream,
)


def forbidden(*_args, **_kwargs):
    raise AssertionError("Unexpected network or direct-device operation")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(("STACKCHAN_", "FISH_AUDIO_")) or name in {
            "MAC_IP", "TTS_ENGINE", "AUDIO_SERVE_PORT", "EDGE_TTS_BIN"
        }:
            monkeypatch.delenv(name)
    monkeypatch.setattr(stackchan_config, "load_dotenv", lambda: None)
    monkeypatch.setattr(stackchan_config, "resolve_mac_ip", lambda *_args: "192.0.2.10")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("mcp_server.stackchan_client.socket.create_connection", forbidden)
    monkeypatch.setattr("mcp_server.stackchan_client.socket.socket", forbidden)
    monkeypatch.setattr("mcp_server.stackchan_client.subprocess.run", forbidden)
    monkeypatch.setattr(mcp_tools, "signal_face_tracking", forbidden)
    monkeypatch.setattr(mcp_tools, "start_audio_server", forbidden)
    monkeypatch.setattr(mcp_tools, "publish_wav", forbidden)
    monkeypatch.setattr(mcp_tools, "AUDIO_DIR", tmp_path)
    monkeypatch.setenv("STACKCHAN_OTEL_LOG", str(tmp_path / "telemetry.jsonl"))


@pytest.fixture
def config():
    return replace(
        stackchan_config.load_config(),
        transport="relay",
        relay_token="test-relay-token",
        pcm_first_segment_timeout=0,
        pcm_gain=1,
        pcm_limit=1,
        pcm_declick_samples=0,
        pcm_zero_cross_window=0,
    )


def response(body=None, *, status=200, content=None):
    result = requests.Response()
    result.status_code = status
    result._content = content if content is not None else json.dumps(body or {}).encode()
    return result


@pytest.fixture
def relay_http(monkeypatch):
    calls = []

    def install(handler=None):
        def request(session, method, url, **kwargs):
            assert session.trust_env is False
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"]["Authorization"] == "Bearer test-relay-token"
            assert url.startswith("http://127.0.0.1:8766/")
            assert "test-relay-token" not in url
            call = (method.upper(), url, kwargs)
            calls.append(call)
            return handler(*call) if handler else response({"success": True})

        monkeypatch.setattr("requests.sessions.Session.request", request)
        return calls

    return install


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(func):
            self.tools[func.__name__] = func
            return func
        return register


def registered_tools(config):
    mcp = FakeMCP()
    mcp_tools.register_tools(mcp, StackchanClient(config), config, lambda **kwargs: kwargs)
    return mcp.tools


def test_config_default_direct_and_relay_skips_route_detection(monkeypatch):
    assert stackchan_config.load_config().transport == "direct"
    monkeypatch.setenv("STACKCHAN_TRANSPORT", "relay")
    monkeypatch.setenv("STACKCHAN_RELAY_TOKEN", "test-relay-token")
    monkeypatch.setattr(stackchan_config, "resolve_mac_ip", forbidden)
    config = stackchan_config.load_config()
    assert config.transport == "relay"
    assert config.mac_ip == "127.0.0.1"
    assert StackchanClient(config).base_url == "http://127.0.0.1:8766/device"
    summary = json.dumps(stackchan_config.config_summary(config))
    assert "test-relay-token" not in summary
    assert "test-relay-token" not in repr(config)
    assert stackchan_config.config_summary(config)["stackchan"]["relay_token_configured"]


def test_config_rejects_unknown_transport(monkeypatch):
    monkeypatch.setenv("STACKCHAN_TRANSPORT", "relai")
    with pytest.raises(ValueError, match="direct or relay"):
        stackchan_config.load_config()


@pytest.mark.parametrize("url", [
    "http://192.0.2.20:8766", "https://example.com", "http://user:secret@localhost:8766",
    "http://localhost:8766?token=secret", "http://localhost:8766/other", "http://localhost#token",
])
def test_relay_rejects_nonlocal_or_credential_urls(config, url):
    with pytest.raises(ValueError, match="loopback HTTP origin"):
        StackchanClient(replace(config, relay_url=url))


def test_relay_requires_token(config):
    with pytest.raises(ValueError, match="TOKEN is required"):
        StackchanClient(replace(config, relay_token=""))


@pytest.mark.parametrize("method,args,path,verb", [
    ("move", (1, 2, 3), "/move", "POST"),
    ("gesture", ("home",), "/home", "POST"),
    ("gesture", ("nod",), "/nod", "POST"),
    ("gesture", ("shake",), "/shake", "POST"),
    ("set_face", ("happy",), "/face", "POST"),
    ("read_env", (), "/env", "GET"),
    ("servo_status", (), "/servo/status", "GET"),
    ("audio_status", (), "/audio/status", "GET"),
    ("playback_status", (), "/playback/status", "GET"),
    ("start_camera_session", (), "/camera/session", "POST"),
    ("camera_status", (), "/camera/status", "GET"),
])
def test_device_methods_use_authenticated_relay(config, relay_http, monkeypatch, method, args, path, verb):
    monkeypatch.setenv("STACKCHAN_HTTP_TRANSPORT", "curl")
    calls = relay_http()
    assert getattr(StackchanClient(config), method)(*args) == {"success": True}
    assert len(calls) == 1
    assert calls[0][:2] == (verb, f"http://127.0.0.1:8766/device{path}")


def test_relay_preserves_command_arguments(config, relay_http):
    calls = relay_http()
    StackchanClient(config).move(1, 2, 3, interrupt_gesture=False)
    assert calls[0][2]["json"] == {"x": 1, "y": 2, "speed": 3, "interrupt_gesture": False}


def test_relay_binary_snapshot(config, relay_http):
    content = b"\xff\xd8\x00binary\n\xff\xd9"
    calls = relay_http(lambda *_args: response(content=content))
    client = StackchanClient(config)
    assert client.snapshot_once(preview=False, camera_session=True) == (content, len(content))
    assert calls[0][1].endswith("/snapshot?preview=0&session=1")
    assert calls[0][2]["timeout"] == 22.0
    assert len(calls) == 1


def test_relay_status_and_health_never_probe_device(config, relay_http):
    calls = relay_http(lambda *_args: response({"online": True, "busy": True, "last_seen_age": 1.5}))
    tools = registered_tools(config)
    assert "online=True busy=True" in tools["stackchan_status"]()
    health = json.loads(tools["stackchan_health"]())
    assert health["ok"] is True
    assert health["device"]["relay"]["busy"] is True
    assert health["device"]["reachability"] == "responding"
    assert len(calls) == 2
    assert all(call[1] == "http://127.0.0.1:8766/relay/status" for call in calls)
    assert all(call[2]["timeout"] == 8 for call in calls)


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_relay_health_preserves_unconfirmed_availability(config, relay_http, error):
    def fail(*_args):
        raise error("relay unavailable")
    calls = relay_http(fail)
    tools = registered_tools(config)
    assert "Availability unconfirmed" in tools["stackchan_status"]()
    health = json.loads(tools["stackchan_health"]())
    assert health["ok"] is False
    assert health["device"]["reachability"] == "unconfirmed"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [302, 401, 409, 413, 503, 504])
def test_relay_errors_and_redirects_never_retry(config, relay_http, status):
    calls = relay_http(lambda *_args: response({"error": "rejected"}, status=status))
    with pytest.raises(requests.HTTPError):
        StackchanClient(config).set_face("calm")
    assert len(calls) == 1


def test_relay_forbids_direct_requests_url_playback_and_raw_pcm(config, tmp_path):
    client = StackchanClient(config)
    with pytest.raises(ValueError, match="/device/"):
        client.request("get", "http://192.0.2.20/audio/status", timeout=1)
    with pytest.raises(ValueError, match="PCM upload"):
        client.play("http://192.0.2.10/test.wav")
    for send in (post_pcm_tcp_stream, post_pcm_udp_stream):
        with pytest.raises(PcmPlaybackError, match="disabled in relay mode"):
            send(client, iter([b"\x00\x00"]), tmp_path, audio_processing)


def test_relay_recording_download_uses_channel_once(config, relay_http):
    calls = relay_http(lambda *_args: response(content=b"RIFF-test-recording"))
    assert StackchanClient(config).get_audio() == b"RIFF-test-recording"
    assert len(calls) == 1
    assert calls[0][1].endswith("/device/audio")


def test_relay_empty_recording_is_none_without_retry(config, relay_http):
    calls = relay_http(lambda *_args: response({"error": "no audio"}, status=404))
    assert StackchanClient(config).get_audio() is None
    assert len(calls) == 1


def test_relay_recording_transport_error_stays_visible(config, relay_http):
    calls = relay_http(lambda *_args: response({"error": "offline"}, status=503))
    with pytest.raises(requests.HTTPError):
        StackchanClient(config).get_audio()
    assert len(calls) == 1


def test_pcm_upload_is_bounded_ordered_and_final_once(config, relay_http, tmp_path):
    calls = relay_http(lambda _method, url, _kwargs: response({
        "success": True, "staged": parse_qs(urlsplit(url).query)["final"] == ["0"]
    }))
    config = replace(config, pcm_segment_bytes=256 * 1024)
    pcm = b"\x01\x00" * (48 * 1024 + 1)
    result = post_pcm_stream(StackchanClient(config), iter([pcm]), tmp_path, audio_processing)
    assert result["success"] is True
    assert len(calls) == 3
    assert b"".join(call[2]["data"] for call in calls) == pcm
    assert all(len(call[2]["data"]) <= 48 * 1024 for call in calls)
    queries = [parse_qs(urlsplit(call[1]).query) for call in calls]
    assert [query["seq"] for query in queries] == [["0"], ["1"], ["2"]]
    assert [query["final"] for query in queries] == [["0"], ["0"], ["1"]]
    assert len({query["session"][0] for query in queries}) == 1
    assert all(query["mode"] == ["staged"] for query in queries)
    assert all(call[2]["headers"]["Content-Type"].startswith("audio/x-raw") for call in calls)


@pytest.mark.parametrize("transport", ["auto", "tcp", "udp", "staged"])
def test_relay_fish_stream_ignores_direct_pcm_transport(config, relay_http, monkeypatch, transport):
    config = replace(config, audio_mode="pcm", pcm_transport=transport, fish_audio_key="test-key")
    monkeypatch.setattr(audio_processing, "iter_fish_pcm_stream", lambda *_args: iter([b"\x00\x00"]))
    monkeypatch.setattr(audio_processing, "generate_tts", forbidden)
    calls = relay_http()
    assert "is saying" in registered_tools(config)["stackchan_say"]("hello")
    assert len(calls) == 1
    assert "/device/play/pcm?" in calls[0][1]


@pytest.mark.parametrize("audio_mode", ["wav", "auto", "pcm"])
@pytest.mark.parametrize("tts_engine", ["edge-tts", "fish-audio"])
def test_relay_generated_wav_uploads_pcm_without_host_fetch(config, relay_http, monkeypatch, tmp_path, audio_mode, tts_engine):
    config = replace(config, audio_mode=audio_mode, tts_engine=tts_engine, audio_publish_target="unused:audio")
    pcm = b"\x00\x00\x01\x00"
    wav_path = tmp_path / "tts.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)
    monkeypatch.setattr(audio_processing, "generate_tts", lambda *_args: wav_path)
    calls = relay_http()
    assert "is saying" in registered_tools(config)["stackchan_say"]("hello")
    assert len(calls) == 1
    assert calls[0][2]["data"] == pcm
    assert "/device/play/pcm?" in calls[0][1]


@pytest.mark.parametrize("status", [409, 504])
def test_relay_speech_failure_has_no_fallback_or_replay(config, relay_http, monkeypatch, status):
    config = replace(config, audio_mode="auto", fish_audio_key="test-key")
    monkeypatch.setattr(audio_processing, "iter_fish_pcm_stream", lambda *_args: iter([b"\x00\x00"]))
    monkeypatch.setattr(audio_processing, "generate_tts", forbidden)
    calls = relay_http(lambda *_args: response({"error": "unavailable"}, status=status))
    result = registered_tools(config)["stackchan_say"]("hello")
    assert "failed" in result.lower()
    assert "is saying" not in result
    assert len(calls) == 1


def test_direct_request_retains_original_url_body_and_timeout(config, monkeypatch):
    calls = []
    def direct_post(url, **kwargs):
        calls.append((url, kwargs))
        return response({"success": True})
    monkeypatch.setattr(requests, "post", direct_post)
    client = StackchanClient(replace(config, transport="direct", stackchan_ip="192.0.2.20"))
    assert client.move(1, 2, 3) == {"success": True}
    assert calls == [("http://192.0.2.20:80/move", {"json": {"x": 1, "y": 2, "speed": 3}, "timeout": 5})]


def test_token_file_overrides_environment_without_exposure(monkeypatch, tmp_path):
    token_file = tmp_path / "relay-token"
    token_file.write_text("  token-from-protected-test-file\n")
    monkeypatch.setenv("STACKCHAN_TRANSPORT", "relay")
    monkeypatch.setenv("STACKCHAN_RELAY_TOKEN", "environment-token")
    monkeypatch.setenv("STACKCHAN_RELAY_TOKEN_FILE", str(token_file))
    config = stackchan_config.load_config()
    assert config.relay_token == "token-from-protected-test-file"
    summary = json.dumps(stackchan_config.config_summary(config))
    assert config.relay_token not in summary
    assert str(token_file) not in summary


def test_missing_token_file_fails_closed_only_in_relay_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_RELAY_TOKEN_FILE", str(tmp_path / "missing"))
    assert stackchan_config.load_config().transport == "direct"
    monkeypatch.setenv("STACKCHAN_TRANSPORT", "relay")
    with pytest.raises(ValueError, match="Could not read STACKCHAN_RELAY_TOKEN_FILE"):
        stackchan_config.load_config()


def test_empty_token_file_does_not_fall_back_to_environment(monkeypatch, tmp_path):
    token_file = tmp_path / "empty-token"
    token_file.write_text("\n")
    monkeypatch.setenv("STACKCHAN_TRANSPORT", "relay")
    monkeypatch.setenv("STACKCHAN_RELAY_TOKEN", "environment-token")
    monkeypatch.setenv("STACKCHAN_RELAY_TOKEN_FILE", str(token_file))
    with pytest.raises(ValueError, match="TOKEN is required"):
        StackchanClient(stackchan_config.load_config())


def test_malformed_status_does_not_invent_offline(config, relay_http):
    relay_http(lambda *_args: response({"error": "unknown"}))
    result = registered_tools(config)["stackchan_status"]()
    assert "availability unconfirmed" in result.lower()
    assert "online=False" not in result


def test_relay_read_timeout_does_not_replay_speech(config, relay_http, monkeypatch):
    config = replace(config, audio_mode="auto", fish_audio_key="test-key")
    monkeypatch.setattr(audio_processing, "iter_fish_pcm_stream", lambda *_args: iter([b"\x00\x00"]))
    monkeypatch.setattr(audio_processing, "generate_tts", forbidden)
    def timeout(*_args):
        raise requests.Timeout("completion unknown")
    calls = relay_http(timeout)
    assert "failed" in registered_tools(config)["stackchan_say"]("hello").lower()
    assert len(calls) == 1


def test_relay_first_segment_deadline_does_not_bound_whole_upload(config, relay_http, monkeypatch, tmp_path):
    config = replace(config, pcm_segment_bytes=2, pcm_first_segment_timeout=3)
    clock = [0.0]
    monkeypatch.setattr("mcp_server.stackchan_client.time.perf_counter", lambda: clock[0])
    def accepted(*_args):
        clock[0] += 2.0
        return response({"success": True, "staged": True})
    calls = relay_http(accepted)
    result = post_pcm_stream(
        StackchanClient(config), iter([b"\x00\x00"] * 5), tmp_path, audio_processing
    )
    assert result["success"] is True
    assert len(calls) == 5
