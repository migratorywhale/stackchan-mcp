import importlib.util
import sys
import types
import wave
from pathlib import Path

import pytest


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


@pytest.fixture()
def server_module(monkeypatch):
    fake_mcp_package = types.ModuleType("mcp")
    fake_mcp_server = types.ModuleType("mcp.server")
    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp.FastMCP = FakeFastMCP
    fake_fastmcp.Image = lambda data, format: {"data": data, "format": format}

    monkeypatch.setitem(sys.modules, "mcp", fake_mcp_package)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_mcp_server)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_fastmcp)
    monkeypatch.setattr(sys, "argv", ["server.py"])

    module_path = Path(__file__).resolve().parents[1] / "mcp-server" / "server.py"
    spec = importlib.util.spec_from_file_location("stackchan_mcp_server_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_expected_mcp_tools_are_registered(server_module):
    assert set(server_module.mcp.tools) == {
        "stackchan_say",
        "stackchan_listen",
        "stackchan_move",
        "stackchan_nod",
        "stackchan_shake",
        "stackchan_face",
        "stackchan_see",
        "stackchan_home",
        "stackchan_status",
    }


def test_audio_url_uses_configured_host_and_port(server_module):
    server_module.MAC_IP = "192.0.2.10"
    server_module.AUDIO_SERVE_PORT = 5099

    assert server_module.audio_url("hello.wav") == "http://192.0.2.10:5099/hello.wav"


def test_move_clamps_inputs_before_http_call(server_module, monkeypatch):
    calls = []

    def fake_move_raw(x, y, speed):
        calls.append((x, y, speed))
        return {"success": True}

    monkeypatch.setattr(server_module, "stackchan_move_raw", fake_move_raw)

    result = server_module.stackchan_move(x=999, y=-20, speed=250)

    assert calls == [(128, 0, 100)]
    assert "x=128" in result
    assert "y=0" in result
    assert "speed 100%" in result


def test_invalid_face_is_rejected_without_http_call(server_module, monkeypatch):
    def fail_if_called(_expression):
        raise AssertionError("HTTP face setter should not be called for invalid expressions")

    monkeypatch.setattr(server_module, "stackchan_set_face", fail_if_called)

    assert "Unknown expression" in server_module.stackchan_face("surprised")


def test_listen_does_not_consume_audio_when_not_ready(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "stackchan_audio_status", lambda: {"ready": False})

    def fail_if_called():
        raise AssertionError("GET /audio consumes the device buffer and should not be called")

    monkeypatch.setattr(server_module, "stackchan_get_audio", fail_if_called)

    assert "No recording ready" in server_module.stackchan_listen()


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


def test_validate_playback_wav_accepts_expected_format(server_module, tmp_path):
    wav_path = tmp_path / "valid.wav"
    write_wav(wav_path)

    server_module.validate_playback_wav(wav_path)


def test_validate_playback_wav_rejects_wrong_format(server_module, tmp_path):
    wav_path = tmp_path / "stereo.wav"
    write_wav(wav_path, channels=2, frames=b"\x00\x00\x00\x00" * 16)

    with pytest.raises(ValueError, match="unsupported WAV format"):
        server_module.validate_playback_wav(wav_path)


def test_validate_playback_wav_rejects_non_wav(server_module, tmp_path):
    wav_path = tmp_path / "not.wav"
    wav_path.write_text("<html>not audio</html>")

    with pytest.raises(ValueError, match="invalid WAV file"):
        server_module.validate_playback_wav(wav_path)


def test_validate_playback_wav_rejects_truncated_data(server_module, tmp_path):
    wav_path = tmp_path / "truncated.wav"
    write_wav(wav_path)
    wav_path.write_bytes(wav_path.read_bytes()[:-3])

    with pytest.raises(ValueError, match="truncated WAV data|invalid WAV file"):
        server_module.validate_playback_wav(wav_path)


def test_stackchan_say_does_not_play_invalid_generated_wav(server_module, monkeypatch, tmp_path):
    bad_wav = tmp_path / "bad.wav"
    bad_wav.write_bytes(b"not a wav")

    monkeypatch.setattr(server_module, "start_audio_server", lambda: None)
    monkeypatch.setattr(server_module, "generate_tts", lambda _text, _lang: bad_wav)

    def fail_if_called(_url):
        raise AssertionError("Invalid generated WAV should not be sent to /play")

    monkeypatch.setattr(server_module, "stackchan_play", fail_if_called)

    result = server_module.stackchan_say("hello")

    assert "Error" in result
    assert "invalid WAV" in result


def test_tts_edge_publishes_only_validated_final_wav(server_module, monkeypatch, tmp_path):
    audio_dir = tmp_path / "audio"
    temp_dir = audio_dir / ".tmp"
    audio_dir.mkdir()
    temp_dir.mkdir()
    monkeypatch.setattr(server_module, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(server_module, "TEMP_AUDIO_DIR", temp_dir)
    monkeypatch.setattr(server_module, "EDGE_TTS_BIN", "edge-tts")

    def fake_run(args, **_kwargs):
        output_path = Path(args[-1])
        if output_path.suffix == ".mp3":
            output_path.write_bytes(b"fake mp3")
        else:
            write_wav(output_path)

    monkeypatch.setattr(server_module.subprocess, "run", fake_run)

    wav_path = server_module.tts_edge("hello", "en")

    assert wav_path.parent == audio_dir
    assert ".tmp" not in wav_path.parts
    assert wav_path.name.startswith("tts_")
    assert wav_path.suffix == ".wav"
    server_module.validate_playback_wav(wav_path)
    assert not list(temp_dir.iterdir())


def test_validate_pcm_contract_rejects_wrong_format(server_module):
    with pytest.raises(ValueError, match="unsupported PCM format"):
        server_module.validate_pcm_contract(44100, 2, 2)


def test_stackchan_say_streams_pcm_with_fish_audio(server_module, monkeypatch):
    chunks = [b"\x01\x00" * 8, b"\x02\x00" * 8]
    calls = []

    monkeypatch.setattr(server_module, "TTS_ENGINE", "fish-audio")
    monkeypatch.setattr(server_module, "FISH_AUDIO_KEY", "test-key")
    monkeypatch.setattr(server_module, "start_audio_server", lambda: None)
    monkeypatch.setattr(server_module, "iter_fish_pcm_stream", lambda text, lang: iter(chunks))

    def fake_play_pcm(pcm_chunks):
        calls.append(list(pcm_chunks))
        return {"success": True}

    monkeypatch.setattr(server_module, "stackchan_play_pcm", fake_play_pcm)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("WAV fallback should not run when PCM streaming succeeds")

    monkeypatch.setattr(server_module, "generate_tts", fail_if_called)
    monkeypatch.setattr(server_module, "stackchan_play", fail_if_called)

    result = server_module.stackchan_say("hello", "en")

    assert calls == [chunks]
    assert "Fish Audio PCM/en" in result


def test_stackchan_say_falls_back_to_wav_when_pcm_streaming_fails(
    server_module, monkeypatch, tmp_path
):
    wav_path = tmp_path / "fallback.wav"
    write_wav(wav_path)
    played_urls = []

    monkeypatch.setattr(server_module, "TTS_ENGINE", "fish-audio")
    monkeypatch.setattr(server_module, "FISH_AUDIO_KEY", "test-key")
    monkeypatch.setattr(server_module, "MAC_IP", "192.0.2.10")
    monkeypatch.setattr(server_module, "AUDIO_SERVE_PORT", 5099)
    monkeypatch.setattr(server_module, "start_audio_server", lambda: None)
    monkeypatch.setattr(server_module, "iter_fish_pcm_stream", lambda text, lang: iter([b"\x00\x00"]))
    monkeypatch.setattr(
        server_module,
        "stackchan_play_pcm",
        lambda _pcm_chunks: (_ for _ in ()).throw(RuntimeError("pcm failed")),
    )
    monkeypatch.setattr(server_module, "generate_tts", lambda _text, _lang: wav_path)

    def fake_play(url):
        played_urls.append(url)
        return {"success": True}

    monkeypatch.setattr(server_module, "stackchan_play", fake_play)

    result = server_module.stackchan_say("hello", "zh")

    assert played_urls == ["http://192.0.2.10:5099/fallback.wav"]
    assert "Fish Audio/zh" in result


def test_stackchan_play_pcm_posts_binary_payload_with_content_length(server_module, monkeypatch):
    request_kwargs = {}

    class FakeResponse:
        def json(self):
            return {"success": True}

    def fake_post(*args, **kwargs):
        request_kwargs["args"] = args
        request_kwargs["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)

    result = server_module.stackchan_play_pcm(iter([b"\x01\x00", b"\x02\x00"]))

    assert result == {"success": True}
    assert request_kwargs["kwargs"]["data"] == b"\x01\x00\x02\x00"
    assert request_kwargs["kwargs"]["headers"]["Content-Type"].startswith("audio/x-raw")


def test_iter_fish_pcm_stream_requests_pcm_chunks(server_module, monkeypatch):
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

    monkeypatch.setattr(server_module, "FISH_AUDIO_KEY", "test-key")
    monkeypatch.setattr(server_module.requests, "post", fake_post)

    chunks = list(server_module.iter_fish_pcm_stream("hello", "en"))

    assert chunks == [b"\x01\x00", b"\x02\x00"]
    assert request_kwargs["args"] == ("https://api.fish.audio/v1/tts",)
    assert request_kwargs["kwargs"]["json"]["format"] == "pcm"
    assert request_kwargs["kwargs"]["json"]["sample_rate"] == 24000
    assert request_kwargs["kwargs"]["stream"] is True
