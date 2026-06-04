import importlib.util
import os
import struct
import sys
import types
import wave
from pathlib import Path

import pytest

from mcp_server import audio_processing
from mcp_server.audio_server import audio_url
from mcp_server.listening import capture_ready_recording, format_listen_result
from mcp_server.mcp_tools import can_stream_pcm, register_tools
from mcp_server.stackchan_client import PcmPlaybackError, StackchanClient, post_pcm_stream
from mcp_server.stackchan_config import PCM_SAMPLE_WIDTH, StackchanConfig, load_config
from mcp_server.voice_inbox import append_event, clear_events, format_events, read_events
from scripts.stackchan_voice_bridge import load_env_file, should_append_to_inbox

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeFastMCP:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator

    def run(self, *args, **kwargs):
        return None


def make_config(**overrides):
    values = {
        "stackchan_ip": "192.0.2.20",
        "stackchan_port": 80,
        "mac_ip": "192.0.2.10",
        "audio_serve_port": 5099,
        "tts_engine": "fish-audio",
        "audio_mode": "auto",
        "save_pcm": False,
        "pcm_gain": 0.75,
        "pcm_limit": 0.90,
        "pcm_declick_samples": 64,
        "pcm_zero_cross_window": 256,
        "edge_tts_bin": "edge-tts",
        "fish_audio_key": "test-key",
        "fish_audio_model_zh": "zh-model",
        "fish_audio_model_en": "en-model",
    }
    values.update(overrides)
    return StackchanConfig(**values)


def write_wav(
    path: Path,
    *,
    channels: int = 1,
    sample_rate: int = 24000,
    sample_width: int = 2,
    frames: bytes = b"\x00\x00" * 16,
) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setframerate(sample_rate)
        wav.setsampwidth(sample_width)
        wav.writeframes(frames)


def test_server_entrypoint_registers_expected_tools(monkeypatch):
    fake_mcp_package = types.ModuleType("mcp")
    fake_mcp_server = types.ModuleType("mcp.server")
    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp.FastMCP = FakeFastMCP
    fake_fastmcp.Image = lambda data, format: {"data": data, "format": format}

    monkeypatch.setitem(sys.modules, "mcp", fake_mcp_package)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_mcp_server)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_fastmcp)
    monkeypatch.setattr(sys, "argv", ["server.py"])

    module_path = REPO_ROOT / "mcp_server" / "server.py"
    spec = importlib.util.spec_from_file_location("mcp_server.server_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    mcp = module.create_mcp(make_config())

    assert set(mcp.tools) == {
        "stackchan_say",
        "stackchan_listen",
        "stackchan_move",
        "stackchan_nod",
        "stackchan_shake",
        "stackchan_face",
        "stackchan_see",
        "stackchan_home",
        "stackchan_status",
        "stackchan_playback_status",
        "stackchan_voice_inbox",
        "stackchan_voice_inbox_clear",
    }


def test_audio_url_uses_configured_host_and_port():
    assert audio_url("192.0.2.10", 5099, "hello.wav") == "http://192.0.2.10:5099/hello.wav"


def test_invalid_pcm_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PCM_GAIN", "loud")
    monkeypatch.setenv("STACKCHAN_PCM_LIMIT", "hot")
    monkeypatch.setenv("STACKCHAN_PCM_DECLICK_SAMPLES", "many")
    monkeypatch.setenv("STACKCHAN_PCM_ZERO_CROSS_WINDOW", "wide")

    config = load_config()

    assert config.pcm_gain == 0.75
    assert config.pcm_limit == 0.90
    assert config.pcm_declick_samples == 64
    assert config.pcm_zero_cross_window == 256


def test_voice_bridge_env_loader_does_not_override_existing_values(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# comments are ignored",
                "STACKCHAN_IP=192.0.2.55",
                "export MAC_IP='192.0.2.99'",
                "FISH_AUDIO_KEY=new-key",
            ]
        )
    )
    monkeypatch.setenv("FISH_AUDIO_KEY", "existing-key")

    load_env_file(env_path)

    assert os.environ["STACKCHAN_IP"] == "192.0.2.55"
    assert os.environ["MAC_IP"] == "192.0.2.99"
    assert os.environ["FISH_AUDIO_KEY"] == "existing-key"


def test_voice_bridge_only_appends_non_empty_transcripts_to_inbox():
    assert should_append_to_inbox({"type": "transcript", "text": "小记，你好。"})
    assert not should_append_to_inbox({"type": "transcript", "text": ""})
    assert not should_append_to_inbox({"type": "transcript", "text": "   "})
    assert not should_append_to_inbox({"type": "idle", "text": "小记，你好。"})


def test_voice_inbox_appends_reads_formats_and_clears(tmp_path):
    inbox = tmp_path / "voice_inbox.jsonl"
    append_event(
        {
            "timestamp": "2026-06-05T00:00:00+09:00",
            "text": "小记，你好。",
            "duration": 3.2,
            "detected_language": "Chinese",
            "wav_path": "/tmp/stackchan_audio/rec.wav",
        },
        inbox,
    )

    events = read_events(path=inbox)

    assert events[0]["text"] == "小记，你好。"
    formatted = format_events(events)
    assert "小记，你好。" in formatted
    assert "/tmp/stackchan_audio/rec.wav" in formatted

    clear_events(inbox)

    assert read_events(path=inbox) == []


def test_voice_inbox_mcp_tools(monkeypatch, tmp_path):
    inbox = tmp_path / "voice_inbox.jsonl"
    append_event({"timestamp": "now", "text": "测试", "duration": 1, "wav_path": "/tmp/a.wav"}, inbox)
    monkeypatch.setenv("STACKCHAN_VOICE_INBOX", str(inbox))

    mcp = FakeFastMCP()
    register_tools(mcp, object(), make_config(), lambda data, format: {"data": data, "format": format})

    assert "测试" in mcp.tools["stackchan_voice_inbox"]()
    assert "cleared" in mcp.tools["stackchan_voice_inbox_clear"]()
    assert "No Stack-chan voice transcripts" in mcp.tools["stackchan_voice_inbox"]()


def test_validate_playback_wav_accepts_expected_format(tmp_path):
    wav_path = tmp_path / "valid.wav"
    write_wav(wav_path)

    audio_processing.validate_playback_wav(wav_path)


def test_validate_playback_wav_rejects_wrong_format(tmp_path):
    wav_path = tmp_path / "stereo.wav"
    write_wav(wav_path, channels=2, frames=b"\x00\x00\x00\x00" * 16)

    with pytest.raises(ValueError, match="unsupported WAV format"):
        audio_processing.validate_playback_wav(wav_path)


def test_validate_playback_wav_rejects_non_wav(tmp_path):
    wav_path = tmp_path / "not.wav"
    wav_path.write_text("<html>not audio</html>")

    with pytest.raises(ValueError, match="invalid WAV file"):
        audio_processing.validate_playback_wav(wav_path)


def test_condition_pcm_chunk_applies_gain_and_limit():
    chunk = struct.pack("<hhhh", 10000, -10000, 32767, -32768)

    conditioned, limited = audio_processing.condition_pcm_chunk(chunk, gain=1.0, limit=0.5)

    assert struct.unpack("<hhhh", conditioned) == (10000, -10000, 16383, -16383)
    assert limited == 2


def test_declick_pcm_segment_smooths_segment_start():
    segment = struct.pack("<hhh", 3000, 3000, 3000)

    declicked, changed = audio_processing.declick_pcm_segment(segment, -3000, 2)

    assert struct.unpack("<hhh", declicked) == (-1000, 1000, 3000)
    assert changed == 2


def test_choose_pcm_segment_cut_prefers_zero_crossing():
    samples = [1000, 800, 400, -20, 20, 900, 1000]
    buffer = bytearray(struct.pack("<" + "h" * len(samples), *samples))

    cut = audio_processing.choose_pcm_segment_cut(buffer, 6 * PCM_SAMPLE_WIDTH, 4)

    assert cut == 3 * PCM_SAMPLE_WIDTH


def test_iter_fish_pcm_stream_requests_pcm_chunks(monkeypatch):
    request_kwargs = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 4096
            yield b"\x01\x00"
            yield b""
            yield b"\x02\x00"

    def fake_post(*args, **kwargs):
        request_kwargs["args"] = args
        request_kwargs["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(audio_processing.requests, "post", fake_post)

    chunks = list(audio_processing.iter_fish_pcm_stream("hello", "en", make_config()))

    assert chunks == [b"\x01\x00", b"\x02\x00"]
    assert request_kwargs["args"] == ("https://api.fish.audio/v1/tts",)
    assert request_kwargs["kwargs"]["json"]["format"] == "pcm"
    assert request_kwargs["kwargs"]["json"]["sample_rate"] == 24000
    assert request_kwargs["kwargs"]["stream"] is True


def test_stackchan_client_posts_move_request(monkeypatch):
    calls = []

    class FakeResponse:
        def json(self):
            return {"success": True}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", fake_post)

    result = StackchanClient(make_config()).move(1, 2, 3)

    assert result == {"success": True}
    assert calls == [
        (
            "http://192.0.2.20:80/move",
            {"json": {"x": 1, "y": 2, "speed": 3}, "timeout": 5},
        )
    ]


def test_post_pcm_stream_posts_binary_payload_with_content_length(monkeypatch, tmp_path):
    request_kwargs = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    def fake_post(*args, **kwargs):
        request_kwargs["args"] = args
        request_kwargs["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", fake_post)

    result = post_pcm_stream(
        StackchanClient(make_config()),
        iter([struct.pack("<h", 1000), struct.pack("<h", -1000)]),
        tmp_path,
        audio_processing,
    )

    assert result["success"] is True
    assert result["segments"] == 1
    assert request_kwargs["kwargs"]["data"] == struct.pack("<hh", 750, -750)
    assert isinstance(request_kwargs["kwargs"]["data"], bytes)
    assert request_kwargs["kwargs"]["headers"]["Content-Type"].startswith("audio/x-raw")
    assert "final=1" in request_kwargs["args"][0]


def test_post_pcm_stream_rejects_oversized_payload_before_http_post(monkeypatch, tmp_path):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Oversized PCM should be rejected before HTTP post")

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", fail_if_called)
    client = StackchanClient(make_config())

    with pytest.raises(ValueError, match="PCM payload too large"):
        post_pcm_stream(client, iter([b"\x00" * (2 * 1024 * 1024 + 2)]), tmp_path, audio_processing)


def test_post_pcm_stream_raises_for_http_error(monkeypatch, tmp_path):
    class FakeResponse:
        text = "{\"success\":false,\"error\":\"playback busy\"}"

        def raise_for_status(self):
            error = __import__("requests").HTTPError("409 Client Error")
            error.response = self
            raise error

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(PcmPlaybackError, match="PCM segment HTTP failed"):
        post_pcm_stream(StackchanClient(make_config()), iter([b"\x00\x00"]), tmp_path, audio_processing)


def test_tools_move_clamps_inputs_before_http_call():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def move(self, x, y, speed):
            self.calls.append((x, y, speed))
            return {"success": True}

    client = FakeClient()
    mcp = FakeFastMCP()
    register_tools(mcp, client, make_config(), lambda data, format: {"data": data, "format": format})

    result = mcp.tools["stackchan_move"](x=999, y=-20, speed=250)

    assert client.calls == [(128, 0, 100)]
    assert "x=128" in result
    assert "y=0" in result
    assert "speed 100%" in result


def test_invalid_face_is_rejected_without_http_call():
    class FakeClient:
        def set_face(self, _expression):
            raise AssertionError("HTTP face setter should not be called for invalid expressions")

    mcp = FakeFastMCP()
    register_tools(mcp, FakeClient(), make_config(), lambda data, format: {"data": data, "format": format})

    assert "Unknown expression" in mcp.tools["stackchan_face"]("surprised")


def test_listen_does_not_consume_audio_when_not_ready():
    class FakeClient:
        def audio_status(self):
            return {"ready": False}

        def get_audio(self):
            raise AssertionError("GET /audio consumes the device buffer and should not be called")

    mcp = FakeFastMCP()
    register_tools(mcp, FakeClient(), make_config(), lambda data, format: {"data": data, "format": format})

    assert "No recording ready" in mcp.tools["stackchan_listen"]()


def test_capture_ready_recording_does_not_consume_audio_when_not_ready(tmp_path):
    class FakeClient:
        def audio_status(self):
            return {"ready": False, "mode": "mcp"}

        def get_audio(self):
            raise AssertionError("GET /audio consumes the device buffer and should not be called")

    result = capture_ready_recording(FakeClient(), make_config(), audio_dir=tmp_path)

    assert result == {
        "ready": False,
        "consumed": False,
        "status": {"ready": False, "mode": "mcp"},
    }
    assert "No recording ready" in format_listen_result(result)


def test_capture_ready_recording_writes_wav_and_transcribes(monkeypatch, tmp_path):
    class FakeClient:
        def audio_status(self):
            return {"ready": True, "mode": "mcp"}

        def get_audio(self):
            return b"RIFF-test-wav"

    def fake_transcribe(wav_path, lang, config):
        assert wav_path.read_bytes() == b"RIFF-test-wav"
        assert lang == "zh"
        assert config.fish_audio_key == "test-key"
        return {"text": "你好，Stackchan", "duration": 1.25, "language": "zh"}

    monkeypatch.setattr(audio_processing, "transcribe_audio", fake_transcribe)

    result = capture_ready_recording(FakeClient(), make_config(), audio_dir=tmp_path)

    assert result["ready"] is True
    assert result["consumed"] is True
    assert result["audio_bytes"] == len(b"RIFF-test-wav")
    assert result["text"] == "你好，Stackchan"
    assert result["duration"] == 1.25
    assert Path(result["wav_path"]).exists()
    assert "你好，Stackchan" in format_listen_result(result)


def test_capture_ready_recording_requires_fish_key_before_consuming_audio(tmp_path):
    class FakeClient:
        def audio_status(self):
            return {"ready": True, "mode": "mcp"}

        def get_audio(self):
            raise AssertionError("GET /audio should not be called without ASR credentials")

    result = capture_ready_recording(
        FakeClient(),
        make_config(fish_audio_key=""),
        audio_dir=tmp_path,
    )

    assert result["ready"] is True
    assert result["consumed"] is False
    assert "Fish Audio key is not configured" in result["error"]
    assert "Fish Audio key is not configured" in format_listen_result(result)


def test_playback_status_formats_runtime_diagnostics():
    class FakeClient:
        def playback_status(self):
            return {
                "kind": "pcm",
                "playing": True,
                "queued_pcm_segments": 2,
                "queued_pcm_bytes": 98304,
                "audio_queue_depth": 1,
                "mic_state": "idle",
                "gesture": "none",
                "free_heap": 123456,
                "free_psram": 654321,
            }

    mcp = FakeFastMCP()
    register_tools(mcp, FakeClient(), make_config(), lambda data, format: {"data": data, "format": format})

    result = mcp.tools["stackchan_playback_status"]()

    assert "kind=pcm" in result
    assert "pcm_queue=2/98304B" in result
    assert "psram=654321" in result


def test_can_stream_pcm_requires_fish_credentials():
    assert can_stream_pcm(make_config()) is True
    assert can_stream_pcm(make_config(audio_mode="wav")) is False
    assert can_stream_pcm(make_config(tts_engine="edge-tts")) is False
    assert can_stream_pcm(make_config(fish_audio_key="")) is False
