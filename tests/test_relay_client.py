import json
import os
import wave
from dataclasses import replace
from unittest.mock import Mock
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

RELAY_BUSY_BODY = {"error": "Device channel busy; command was not queued"}


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


@pytest.fixture
def retry_sleep(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", sleep)
    return sleep


class OnePassPcm:
    def __init__(self, chunks):
        self.chunks = chunks
        self.iterations = 0
        self.consumed = []

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1, "PCM input must not restart during retry"
        for chunk in self.chunks:
            self.consumed.append(chunk)
            yield chunk


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


@pytest.mark.parametrize("segment_count,busy_seq", [(1, 0), (3, 0), (3, 1), (3, 2)])
def test_relay_pcm_busy_retries_identical_segment_once(
    config, relay_http, retry_sleep, monkeypatch, tmp_path, segment_count, busy_seq
):
    config = replace(config, pcm_segment_bytes=4, pcm_declick_samples=2, save_pcm=True)
    chunks = [b"\x01\x00\x02\x00", b"\x20\x00\x30\x00", b"\x04\x00\x05\x00"][:segment_count]
    stream = OnePassPcm(chunks)
    condition = Mock(wraps=audio_processing.condition_pcm_chunk)
    declick = Mock(wraps=audio_processing.declick_pcm_segment)
    monkeypatch.setattr(audio_processing, "condition_pcm_chunk", condition)
    monkeypatch.setattr(audio_processing, "declick_pcm_segment", declick)
    refused_at = None
    consumed_at_refusal = None

    def accepted_after_busy(_method, url, _kwargs):
        nonlocal refused_at, consumed_at_refusal
        query = parse_qs(urlsplit(url).query)
        if int(query["seq"][0]) == busy_seq:
            if refused_at is None:
                retry_sleep.assert_not_called()
                refused_at = len(calls) - 1
                consumed_at_refusal = list(stream.consumed)
                return response(RELAY_BUSY_BODY, status=409)
            retry_sleep.assert_called_once_with(1.5)
            assert stream.consumed == consumed_at_refusal
        return response({"success": True, "staged": query["final"] == ["0"]})

    calls = relay_http(accepted_after_busy)
    result = post_pcm_stream(StackchanClient(config), stream, tmp_path, audio_processing)

    assert result["success"] is True
    assert result["segments"] == segment_count
    assert result["total_bytes"] == sum(map(len, chunks))
    assert result["declicked_samples"] == 2 * (segment_count - 1)
    retry_sleep.assert_called_once_with(1.5)
    assert len(calls) == segment_count + 1
    assert refused_at == busy_seq
    assert calls[busy_seq] == calls[busy_seq + 1]
    queries = [parse_qs(urlsplit(call[1]).query) for call in calls]
    expected_seqs = list(range(segment_count))
    expected_seqs.insert(busy_seq, busy_seq)
    assert [int(query["seq"][0]) for query in queries] == expected_seqs
    assert [query["final"][0] for query in queries] == [
        "1" if seq == segment_count - 1 else "0" for seq in expected_seqs
    ]
    assert {query["session"][0] for query in queries} == {result["session"]}
    for (_, _, kwargs), query in zip(calls, queries, strict=True):
        assert kwargs["headers"]["X-Stackchan-Pcm-Session"] == query["session"][0]
        assert kwargs["headers"]["X-Stackchan-Pcm-Seq"] == query["seq"][0]
        assert kwargs["headers"]["X-Stackchan-Pcm-Final"] == query["final"][0]
        assert kwargs["headers"]["X-Stackchan-Pcm-Mode"] == "staged"
        assert query["mode"] == ["staged"]
    assert stream.iterations == 1
    assert stream.consumed == chunks
    assert condition.call_count == segment_count
    assert declick.call_count == segment_count
    assert (tmp_path / f"diag_{result['session']}.pcm").read_bytes() == b"".join(chunks)


@pytest.mark.parametrize("first_busy_seq,second_busy_seq", [(0, 1), (0, 2), (1, 2)])
def test_relay_pcm_retry_budget_is_shared_across_segments(
    config, relay_http, retry_sleep, tmp_path, first_busy_seq, second_busy_seq
):
    config = replace(config, pcm_segment_bytes=4)
    refused = set()

    def busy_once_per_segment(_method, url, _kwargs):
        query = parse_qs(urlsplit(url).query)
        seq = int(query["seq"][0])
        if seq in {first_busy_seq, second_busy_seq} and seq not in refused:
            refused.add(seq)
            return response(RELAY_BUSY_BODY, status=409)
        return response({"success": True, "staged": query["final"] == ["0"]})

    calls = relay_http(busy_once_per_segment)
    with pytest.raises(PcmPlaybackError) as caught:
        post_pcm_stream(
            StackchanClient(config), iter([b"\x01\x00" * 6]), tmp_path, audio_processing
        )
    retry_sleep.assert_called_once_with(1.5)
    expected_seqs = list(range(second_busy_seq + 1))
    expected_seqs.insert(first_busy_seq, first_busy_seq)
    assert [int(parse_qs(urlsplit(call[1]).query)["seq"][0]) for call in calls] == expected_seqs
    assert calls[first_busy_seq] == calls[first_busy_seq + 1]
    assert caught.value.started is False
    cause = caught.value.__cause__
    assert isinstance(cause, requests.HTTPError)
    assert cause.response.status_code == 409
    assert cause.response.json() == RELAY_BUSY_BODY


@pytest.mark.parametrize("second_outcome", ["busy", "timeout", "503", "504"])
def test_relay_pcm_second_attempt_failure_stops_without_extra_retry(
    config, relay_http, retry_sleep, tmp_path, second_outcome
):
    config = replace(config, pcm_segment_bytes=4)
    timeout = requests.ReadTimeout("completion unknown")

    def fail_twice(*_args):
        if len(calls) == 1:
            return response(RELAY_BUSY_BODY, status=409)
        if len(calls) == 2:
            retry_sleep.assert_called_once_with(1.5)
            if second_outcome == "timeout":
                raise timeout
            status = 409 if second_outcome == "busy" else int(second_outcome)
            return response(RELAY_BUSY_BODY, status=status)
        return response({"success": True})

    calls = relay_http(fail_twice)
    stream = OnePassPcm([b"\x01\x00\x02\x00"] * 3)
    with pytest.raises(PcmPlaybackError) as caught:
        post_pcm_stream(StackchanClient(config), stream, tmp_path, audio_processing)
    assert len(calls) == 2
    assert calls[0] == calls[1]
    retry_sleep.assert_called_once_with(1.5)
    assert stream.iterations == 1
    assert len(stream.consumed) == 2
    assert caught.value.started is False
    if second_outcome == "timeout":
        assert caught.value.__cause__ is timeout
    else:
        assert isinstance(caught.value.__cause__, requests.HTTPError)


def test_relay_pcm_retry_budget_resets_for_next_utterance(config, relay_http, retry_sleep, tmp_path):
    def busy_then_accepted(*_args):
        if len(calls) % 2:
            return response(RELAY_BUSY_BODY, status=409)
        return response({"success": True})

    calls = relay_http(busy_then_accepted)
    client = StackchanClient(config)
    for _ in range(2):
        assert post_pcm_stream(client, iter([b"\x01\x00"]), tmp_path, audio_processing)["success"]
    assert len(calls) == 4
    assert [call.args for call in retry_sleep.call_args_list] == [(1.5,), (1.5,)]
    assert calls[0] == calls[1]
    assert calls[2] == calls[3]
    assert calls[0][1] != calls[2][1]


@pytest.mark.parametrize("status,body", [
    pytest.param(409, {"error": "busy"}, id="generic-conflict"),
    pytest.param(409, {"success": False, "error": "playback busy"}, id="device-busy"),
    pytest.param(409, {"success": False, "error": "pcm session mismatch"}, id="device-session"),
    pytest.param(409, {"success": False, "error": "pcm seq invalid"}, id="device-sequence"),
    pytest.param(409, {**RELAY_BUSY_BODY, "success": False}, id="extra-json-key"),
    pytest.param(409, {"error": RELAY_BUSY_BODY["error"] + "."}, id="different-message"),
    pytest.param(409, [RELAY_BUSY_BODY], id="json-list"),
    pytest.param(409, RELAY_BUSY_BODY["error"], id="json-string"),
    pytest.param(409, None, id="json-null"),
    pytest.param(503, RELAY_BUSY_BODY, id="503-exact-body"),
    pytest.param(504, RELAY_BUSY_BODY, id="504-exact-body"),
    pytest.param(200, RELAY_BUSY_BODY, id="no-http-error"),
])
def test_relay_pcm_non_admission_errors_never_retry(
    config, relay_http, retry_sleep, tmp_path, status, body
):
    calls = relay_http(lambda *_args: response(status=status, content=json.dumps(body).encode()))
    with pytest.raises(PcmPlaybackError):
        post_pcm_stream(StackchanClient(config), iter([b"\x01\x00"]), tmp_path, audio_processing)
    assert len(calls) == 1
    retry_sleep.assert_not_called()


@pytest.mark.parametrize("status", [200, 409])
@pytest.mark.parametrize("content", [b"", b"{", b"Device channel busy; command was not queued"])
def test_relay_pcm_malformed_json_never_retries(config, relay_http, retry_sleep, tmp_path, status, content):
    calls = relay_http(lambda *_args: response(status=status, content=content))
    with pytest.raises(PcmPlaybackError):
        post_pcm_stream(StackchanClient(config), iter([b"\x01\x00"]), tmp_path, audio_processing)
    assert len(calls) == 1
    retry_sleep.assert_not_called()


@pytest.mark.parametrize("error", [
    requests.Timeout,
    requests.ReadTimeout,
    requests.ConnectTimeout,
    requests.ConnectionError,
    requests.RequestException,
])
def test_relay_pcm_non_http_exceptions_never_retry(config, relay_http, retry_sleep, tmp_path, error):
    failure = error("completion unknown", response=response(RELAY_BUSY_BODY, status=409))

    def fail(*_args):
        raise failure

    calls = relay_http(fail)
    with pytest.raises(PcmPlaybackError) as caught:
        post_pcm_stream(StackchanClient(config), iter([b"\x01\x00"]), tmp_path, audio_processing)
    assert caught.value.__cause__ is failure
    assert len(calls) == 1
    retry_sleep.assert_not_called()


def test_relay_pcm_http_error_without_response_never_retries(config, relay_http, retry_sleep, tmp_path):
    failure = requests.HTTPError("409 Device channel busy; command was not queued")

    def fail(*_args):
        raise failure

    calls = relay_http(fail)
    with pytest.raises(PcmPlaybackError) as caught:
        post_pcm_stream(StackchanClient(config), iter([b"\x01\x00"]), tmp_path, audio_processing)
    assert caught.value.__cause__ is failure
    assert len(calls) == 1
    retry_sleep.assert_not_called()


def test_direct_pcm_does_not_retry_exact_relay_busy_body(config, retry_sleep, monkeypatch, tmp_path):
    config = replace(config, transport="direct", stackchan_ip="192.0.2.20")
    post = Mock(return_value=response(RELAY_BUSY_BODY, status=409))
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(PcmPlaybackError):
        post_pcm_stream(StackchanClient(config), iter([b"\x01\x00"]), tmp_path, audio_processing)
    assert post.call_count == 1
    assert post.call_args.args[0].startswith("http://192.0.2.20:80/play/pcm?")
    retry_sleep.assert_not_called()


@pytest.mark.parametrize("method,args", [
    ("move", (1, 2, 3)),
    ("gesture", ("nod",)),
    ("set_face", ("calm",)),
    ("read_env", ()),
    ("audio_status", ()),
    ("playback_status", ()),
    ("start_camera_session", ()),
    ("snapshot_once", ()),
    ("get_audio", ()),
    ("relay_status", ()),
])
def test_non_speech_apis_never_retry_exact_relay_busy_body(
    config, relay_http, retry_sleep, method, args
):
    calls = relay_http(lambda *_args: response(RELAY_BUSY_BODY, status=409))
    with pytest.raises(requests.HTTPError):
        getattr(StackchanClient(config), method)(*args)
    assert len(calls) == 1
    retry_sleep.assert_not_called()


def test_raw_pcm_request_does_not_gain_speech_retry_policy(config, relay_http, retry_sleep):
    calls = relay_http(lambda *_args: response(RELAY_BUSY_BODY, status=409))
    client = StackchanClient(config)
    with pytest.raises(requests.HTTPError):
        client.request("post", f"{client.base_url}/play/pcm", data=b"\x01\x00", timeout=5)
    assert len(calls) == 1
    retry_sleep.assert_not_called()


@pytest.mark.parametrize("tts_path", ["fish-stream", "generated-wav"])
@pytest.mark.parametrize("retry_outcome", ["success", "busy", "timeout"])
def test_registered_say_retries_only_refused_segment_without_reinvoking_tts(
    config, relay_http, retry_sleep, monkeypatch, tmp_path, tts_path, retry_outcome
):
    config = replace(config, audio_mode="auto", pcm_segment_bytes=4, fish_audio_key="test-key")
    pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00\x05\x00\x06\x00"
    stream = OnePassPcm([pcm])
    other_tts = Mock(side_effect=forbidden)
    if tts_path == "fish-stream":
        tts = Mock(return_value=stream)
        wav_stream = Mock(side_effect=forbidden)
        monkeypatch.setattr(audio_processing, "iter_fish_pcm_stream", tts)
        monkeypatch.setattr(audio_processing, "generate_tts", other_tts)
        monkeypatch.setattr(audio_processing, "iter_wav_pcm", wav_stream)
    else:
        config = replace(config, tts_engine="edge-tts")
        wav_path = tmp_path / "tts.wav"
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm)
        tts = Mock(return_value=wav_path)
        wav_stream = Mock(wraps=audio_processing.iter_wav_pcm)
        monkeypatch.setattr(audio_processing, "generate_tts", tts)
        monkeypatch.setattr(audio_processing, "iter_wav_pcm", wav_stream)
        monkeypatch.setattr(audio_processing, "iter_fish_pcm_stream", other_tts)

    attempts = 0

    def later_segment_busy(_method, url, _kwargs):
        nonlocal attempts
        query = parse_qs(urlsplit(url).query)
        if query["seq"] == ["1"]:
            attempts += 1
            assert attempts <= 2, "A refused segment gets at most one retry"
            if attempts == 1 or retry_outcome == "busy":
                return response(RELAY_BUSY_BODY, status=409)
            if retry_outcome == "timeout":
                raise requests.ReadTimeout("completion unknown")
        return response({"success": True, "staged": query["final"] == ["0"]})

    calls = relay_http(later_segment_busy)
    result = registered_tools(config)["stackchan_say"]("hello", lang="en")
    tts.assert_called_once_with("hello", "en", config)
    other_tts.assert_not_called()
    retry_sleep.assert_called_once_with(1.5)
    assert calls[1] == calls[2]
    if tts_path == "fish-stream":
        wav_stream.assert_not_called()
        assert stream.iterations == 1
        assert stream.consumed == [pcm]
    else:
        wav_stream.assert_called_once_with(wav_path)
    if retry_outcome == "success":
        assert "is saying" in result
        expected_seqs = ["0", "1", "1", "2"]
        assert b"".join(call[2]["data"] for index, call in enumerate(calls) if index != 1) == pcm
    else:
        assert "failed" in result.lower()
        assert "is saying" not in result
        expected_seqs = ["0", "1", "1"]
    assert [parse_qs(urlsplit(call[1]).query)["seq"][0] for call in calls] == expected_seqs
    assert len({parse_qs(urlsplit(call[1]).query)["session"][0] for call in calls}) == 1


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
