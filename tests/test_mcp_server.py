import asyncio
import importlib.util
import json
import os
import struct
import sys
import types
import wave
from pathlib import Path
from typing import Any

import pytest
import requests

from mcp_server import audio_processing
from mcp_server.audio_server import audio_url
from mcp_server.listening import capture_ready_recording, format_listen_result
from mcp_server.mcp_tools import (
    can_stream_pcm,
    format_speech_confirmation,
    post_preferred_pcm_stream,
    register_tools,
)
from mcp_server.stackchan_client import (
    PcmPlaybackError,
    StackchanClient,
    post_pcm_stream,
    post_pcm_tcp_stream,
    post_pcm_udp_stream,
)
from mcp_server.stackchan_config import (
    PCM_SAMPLE_WIDTH,
    PCM_SEGMENT_BYTES,
    PCM_UDP_FRAME_BYTES,
    StackchanConfig,
    load_config,
)
from mcp_server.voice_inbox import append_event, clear_events, format_events, read_events
from scripts import (
    stackchan_frontend_session,
    stackchan_frontend_wake,
    stackchan_voice_upload_server,
)
from scripts.stackchan_voice_bridge import (
    load_env_file,
    load_frontend_token,
    should_append_to_inbox,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_telemetry_log(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_OTEL_LOG", str(tmp_path / "otel.jsonl"))


class FakeFastMCP:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.tools = {}

    def tool(self, **kwargs):
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
        "pcm_transport": "tcp",
        "save_pcm": False,
        "pcm_gain": 1.0,
        "pcm_limit": 0.90,
        "pcm_segment_bytes": PCM_SEGMENT_BYTES,
        "pcm_stream_port": 9090,
        "pcm_stream_initial_buffer_bytes": 96 * 1024,
        "pcm_stream_connect_timeout": 3.0,
        "pcm_stream_io_timeout": 30.0,
        "udp_pace_factor": 1.0,
        "max_pcm_payload_bytes": 2 * 1024 * 1024,
        "pcm_declick_samples": 64,
        "pcm_zero_cross_window": 256,
        "http_play_timeout": 5.0,
        "http_audio_timeout": 10.0,
        "http_status_timeout": 3.0,
        "http_command_timeout": 5.0,
        "http_snapshot_warmup_timeout": 5.0,
        "http_snapshot_timeout": 10.0,
        "playback_start_timeout": 5.0,
        "playback_poll_interval": 0.2,
        "pcm_segment_post_timeout": 30.0,
        "pcm_first_segment_timeout": 3.0,
        "fish_tts_timeout": 30.0,
        "fish_asr_timeout": 15.0,
        "fish_stream_chunk_bytes": 4096,
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


def mcp_json_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [mcp_json_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: mcp_json_payload(item) for key, item in value.items()}
    return value


def assert_mcp_json_serializable(value: Any) -> None:
    json.dumps(mcp_json_payload(value))


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
        "stackchan_health",
        "stackchan_config_summary",
        "stackchan_playback_status",
        "stackchan_voice_inbox",
        "stackchan_voice_inbox_clear",
    }


def test_stackchan_see_returns_fastmcp_serializable_image_content(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP, Image

    class FakeClient:
        def snapshot(self):
            return b"\xff\xd8fake-jpeg\xff\xd9", 15

    monkeypatch.setattr("mcp_server.mcp_tools.AUDIO_DIR", tmp_path)
    mcp = FastMCP("stackchan-test")
    register_tools(mcp, FakeClient(), make_config(), Image)

    tool = mcp._tool_manager.get_tool("stackchan_see")
    assert tool is not None
    content = asyncio.run(tool.run({}, convert_result=True))

    assert content[0].type == "image"
    assert content[0].mimeType == "image/jpeg"
    assert content[1].type == "text"
    assert "Photo captured" in content[1].text
    assert_mcp_json_serializable(content)


def test_registered_mcp_tools_return_json_serializable_content(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP, Image

    class FakeClient:
        base_url = "http://192.0.2.20:80"

        def audio_status(self):
            return {"ready": False, "mode": "mcp"}

        def playback_status(self):
            return {
                "kind": "idle",
                "playing": False,
                "queued_pcm_segments": 0,
                "queued_pcm_bytes": 0,
                "audio_queue_depth": 0,
                "download_queue_depth": 0,
                "download_in_flight": False,
                "mic_state": "idle",
                "gesture": "none",
                "free_heap": 123456,
                "free_psram": 654321,
            }

        def move(self, x, y, speed):
            return {"success": True}

        def gesture(self, gesture):
            return {"success": True}

        def set_face(self, face):
            return {"success": True}

        def snapshot(self):
            return b"\xff\xd8contract-jpeg\xff\xd9", 17

        def get_audio(self):
            raise AssertionError("contract test must not consume GET /audio")

    monkeypatch.setattr("mcp_server.mcp_tools.AUDIO_DIR", tmp_path)
    monkeypatch.setenv("STACKCHAN_VOICE_INBOX", str(tmp_path / "voice_inbox.jsonl"))
    mcp = FastMCP("stackchan-contract")
    register_tools(mcp, FakeClient(), make_config(), Image)

    cases = {
        "stackchan_listen": {"lang": "zh"},
        "stackchan_move": {"x": 10, "y": 20, "speed": 30},
        "stackchan_nod": {},
        "stackchan_shake": {},
        "stackchan_face": {"expression": "calm"},
        "stackchan_see": {},
        "stackchan_home": {},
        "stackchan_status": {},
        "stackchan_health": {},
        "stackchan_config_summary": {},
        "stackchan_playback_status": {},
        "stackchan_voice_inbox": {"limit": 10},
        "stackchan_voice_inbox_clear": {},
    }

    for name, arguments in cases.items():
        tool = mcp._tool_manager.get_tool(name)
        assert tool is not None, name
        result = asyncio.run(tool.run(arguments, convert_result=True))
        assert_mcp_json_serializable(result)


def test_audio_url_uses_configured_host_and_port():
    assert audio_url("192.0.2.10", 5099, "hello.wav") == "http://192.0.2.10:5099/hello.wav"


def test_speech_confirmation_omits_transport_diagnostics():
    result = format_speech_confirmation("你好，小克" * 20)

    assert result.startswith("🗣️ Stack-chan is saying: \"")
    assert "transport=" not in result
    assert "timing[" not in result
    assert "…" in result


def test_preferred_pcm_auto_uses_tcp_before_experimental_udp(monkeypatch, tmp_path):
    calls = []

    def fake_tcp(_client, _chunks, _audio_dir, _audio_processing):
        calls.append("tcp")
        return {"success": True, "transport": "tcp", "total_bytes": 2}

    def fake_udp(*_args, **_kwargs):
        raise AssertionError("auto should not use UDP")

    monkeypatch.setattr("mcp_server.mcp_tools.AUDIO_DIR", tmp_path)
    monkeypatch.setattr("mcp_server.mcp_tools.post_pcm_tcp_stream", fake_tcp)
    monkeypatch.setattr("mcp_server.mcp_tools.post_pcm_udp_stream", fake_udp)

    config = make_config(pcm_transport="auto")
    result = post_preferred_pcm_stream(StackchanClient(config), "hello", "zh", config)

    assert calls == ["tcp"]
    assert result["transport"] == "tcp"


def test_preferred_pcm_auto_falls_back_to_staged_when_tcp_rejects_before_start(monkeypatch, tmp_path):
    calls = []

    def fake_tcp(_client, _chunks, _audio_dir, _audio_processing):
        calls.append("tcp")
        raise PcmPlaybackError("busy", started=False)

    def fake_staged(_client, _chunks, _audio_dir, _audio_processing):
        calls.append("staged")
        return {"success": True, "total_bytes": 2}

    monkeypatch.setattr("mcp_server.mcp_tools.AUDIO_DIR", tmp_path)
    monkeypatch.setattr("mcp_server.mcp_tools.post_pcm_tcp_stream", fake_tcp)
    monkeypatch.setattr("mcp_server.mcp_tools.post_pcm_stream", fake_staged)

    config = make_config(pcm_transport="auto")
    result = post_preferred_pcm_stream(StackchanClient(config), "hello", "zh", config)

    assert calls == ["tcp", "staged"]
    assert result["transport"] == "staged"


def test_invalid_pcm_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)
    monkeypatch.setenv("STACKCHAN_PCM_GAIN", "loud")
    monkeypatch.setenv("STACKCHAN_PCM_LIMIT", "hot")
    monkeypatch.setenv("STACKCHAN_PCM_DECLICK_SAMPLES", "many")
    monkeypatch.setenv("STACKCHAN_PCM_ZERO_CROSS_WINDOW", "wide")

    config = load_config()

    assert config.audio_mode == "wav"
    assert config.pcm_gain == 1.0
    assert config.pcm_limit == 0.90
    assert config.pcm_declick_samples == 64
    assert config.pcm_zero_cross_window == 256


def test_udp_pace_factor_clamps_to_lower_and_upper_bounds(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)

    monkeypatch.setenv("STACKCHAN_UDP_PACE_FACTOR", "0.5")
    low_config = load_config()

    monkeypatch.setenv("STACKCHAN_UDP_PACE_FACTOR", "0.85")
    floor_config = load_config()

    monkeypatch.setenv("STACKCHAN_UDP_PACE_FACTOR", "3.0")
    high_config = load_config()

    monkeypatch.delenv("STACKCHAN_UDP_PACE_FACTOR", raising=False)
    default_config = load_config()

    assert low_config.udp_pace_factor == 0.85
    assert floor_config.udp_pace_factor == 0.85
    assert high_config.udp_pace_factor == 1.5
    assert default_config.udp_pace_factor == 1.0


def test_invalid_audio_mode_falls_back_to_auto(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)
    monkeypatch.setenv("STACKCHAN_AUDIO_MODE", "stream")

    config = load_config()

    assert config.audio_mode == "wav"


def test_invalid_pcm_transport_falls_back_to_auto(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)
    monkeypatch.setenv("STACKCHAN_PCM_TRANSPORT", "bananas")

    config = load_config()

    assert config.pcm_transport == "auto"


def test_load_config_defaults_to_wav_and_full_pcm_gain(monkeypatch):
    monkeypatch.delenv("STACKCHAN_AUDIO_MODE", raising=False)
    monkeypatch.delenv("STACKCHAN_PCM_GAIN", raising=False)
    monkeypatch.delenv("STACKCHAN_PCM_LIMIT", raising=False)
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)

    config = load_config()

    assert config.audio_mode == "wav"
    assert config.pcm_transport == "tcp"
    assert config.pcm_gain == 1.0
    assert config.pcm_limit == 0.90


def test_load_config_accepts_explicit_audio_modes(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)

    monkeypatch.setenv("STACKCHAN_AUDIO_MODE", "auto")
    auto_config = load_config()

    monkeypatch.setenv("STACKCHAN_AUDIO_MODE", "pcm")
    pcm_config = load_config()

    assert auto_config.audio_mode == "auto"
    assert pcm_config.audio_mode == "pcm"


def test_load_config_accepts_explicit_pcm_transports(monkeypatch):
    monkeypatch.setattr("mcp_server.stackchan_config.load_dotenv", lambda path=None: None)

    monkeypatch.setenv("STACKCHAN_PCM_TRANSPORT", "udp")
    udp_config = load_config()

    monkeypatch.setenv("STACKCHAN_PCM_TRANSPORT", "auto")
    auto_config = load_config()

    monkeypatch.setenv("STACKCHAN_PCM_TRANSPORT", "staged")
    staged_config = load_config()

    assert udp_config.pcm_transport == "udp"
    assert auto_config.pcm_transport == "auto"
    assert staged_config.pcm_transport == "staged"


def test_config_reads_pcm_and_timeout_env_aliases(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PCM_SEGMENT_BYTES", "32768")
    monkeypatch.setenv("STACKCHAN_PCM_TRANSPORT", "tcp")
    monkeypatch.setenv("STACKCHAN_PCM_STREAM_PORT", "9191")
    monkeypatch.setenv("STACKCHAN_PCM_STREAM_INITIAL_BUFFER_BYTES", "32768")
    monkeypatch.setenv("STACKCHAN_PCM_MAX_PAYLOAD_BYTES", "1048576")
    monkeypatch.setenv("STACKCHAN_PLAYBACK_START_TIMEOUT_SEC", "7.5")
    monkeypatch.setenv("STACKCHAN_PCM_SEGMENT_POST_TIMEOUT_SEC", "22")
    monkeypatch.setenv("STACKCHAN_PCM_FIRST_SEGMENT_TIMEOUT_SEC", "4.5")
    monkeypatch.setenv("STACKCHAN_FISH_STREAM_CHUNK_BYTES", "8192")

    config = load_config()

    assert config.pcm_segment_bytes == 32768
    assert config.pcm_transport == "tcp"
    assert config.pcm_stream_port == 9191
    assert config.pcm_stream_initial_buffer_bytes == 32768
    assert config.max_pcm_payload_bytes == 1048576
    assert config.playback_start_timeout == 7.5
    assert config.pcm_segment_post_timeout == 22
    assert config.pcm_first_segment_timeout == 4.5
    assert config.fish_stream_chunk_bytes == 8192


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
    monkeypatch.delenv("STACKCHAN_IP", raising=False)
    monkeypatch.delenv("MAC_IP", raising=False)
    monkeypatch.setenv("FISH_AUDIO_KEY", "existing-key")

    load_env_file(env_path)

    assert os.environ["STACKCHAN_IP"] == "192.0.2.55"
    assert os.environ["MAC_IP"] == "192.0.2.99"
    assert os.environ["FISH_AUDIO_KEY"] == "existing-key"


def test_voice_bridge_loads_frontend_token_from_agent_host_env(monkeypatch, tmp_path):
    env_path = tmp_path / "relay.env"
    env_path.write_text("AGENT_HOST_TOKEN='agent-secret'\n", encoding="utf-8")
    monkeypatch.delenv("STACKCHAN_FRONTEND_TOKEN", raising=False)
    monkeypatch.setenv("STACKCHAN_FRONTEND_ENV", str(env_path))

    load_frontend_token()

    assert os.environ["STACKCHAN_FRONTEND_TOKEN"] == "agent-secret"


def test_voice_bridge_only_appends_non_empty_transcripts_to_inbox():
    assert should_append_to_inbox({"type": "transcript", "text": "小记，你好。"})
    assert not should_append_to_inbox({"type": "transcript", "text": ""})
    assert not should_append_to_inbox({"type": "transcript", "text": "   "})
    assert not should_append_to_inbox({"type": "idle", "text": "小记，你好。"})


def test_voice_upload_processes_wav_into_transcript_event(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_OTEL_LOG", str(tmp_path / "otel.jsonl"))

    def fake_transcribe(wav_path, lang, config):
        assert wav_path.read_bytes() == b"RIFF-upload-wav"
        assert lang == "zh"
        assert config.fish_audio_key == "test-key"
        return {"text": "从上传来的声音", "duration": 2.5, "language": "zh"}

    event = stackchan_voice_upload_server.process_uploaded_wav(
        b"RIFF-upload-wav",
        make_config(),
        lang="zh",
        audio_dir=tmp_path,
        transcribe_fn=fake_transcribe,
    )

    assert event["type"] == "transcript"
    assert event["source"] == "voice_upload"
    assert event["request_id"]
    assert event["text"] == "从上传来的声音"
    assert event["duration"] == 2.5
    assert event["detected_language"] == "zh"
    assert event["audio_bytes"] == len(b"RIFF-upload-wav")
    assert set(event["timing_ms"]) == {"upload_save", "asr", "process_total"}
    assert Path(event["wav_path"]).exists()
    records = [json.loads(line) for line in (tmp_path / "otel.jsonl").read_text().splitlines()]
    assert records[-1]["event_name"] == "stackchan.voice.asr.completed"
    assert records[-1]["attributes"]["stackchan.request_id"] == event["request_id"]


def test_voice_upload_requires_fish_key_before_processing(tmp_path):
    with pytest.raises(RuntimeError, match="Fish Audio key is not configured"):
        stackchan_voice_upload_server.process_uploaded_wav(
            b"RIFF-upload-wav",
            make_config(fish_audio_key=""),
            audio_dir=tmp_path,
        )


def test_voice_upload_frontend_forwarding_skips_without_config():
    result = stackchan_voice_upload_server.forward_to_frontend(
        {"text": "你好"},
        wake_url="",
        session_id="",
    )

    assert result["ok"] is False
    assert result["skipped"] == "frontend wake not configured"
    assert "wake_total" in result["timing_ms"]


def test_voice_upload_recorder_page_exposes_upload_ui():
    html = stackchan_voice_upload_server.build_recorder_page(
        stackchan_voice_upload_server.ServerOptions(
            lang="zh",
            max_bytes=1024,
            inbox_path=None,
            wake_url="http://127.0.0.1:3200/wake",
            wake_session_id="117067d6-1111-2222-3333-444444444444",
            wake_token="",
            wake_model="",
            wake_timeout=3,
            wake_retries=0,
            wake_retry_delay=0,
            wake_force=True,
            wake_quiet_minutes=0,
            prompt_prefix="[Stack-chan语音输入] ",
            wake_words=("小塔", "机器人"),
            upload_token="secret-token-for-test",
            upload_rate_per_minute=12,
            upload_rate_window_seconds=60,
            allowed_origins=(),
        )
    )

    assert "Stack-chan Voice Upload" in html
    assert "/voice/upload" in html
    assert "X-Stackchan-Upload-Token" in html
    assert "secret-token-for-test" not in html
    assert "小塔 / 机器人" in html


def test_voice_upload_token_authorization():
    assert stackchan_voice_upload_server.is_upload_authorized(
        "/voice/upload",
        {},
        "",
    )
    assert not stackchan_voice_upload_server.is_upload_authorized(
        "/voice/upload",
        {},
        "secret-token",
    )
    assert stackchan_voice_upload_server.is_upload_authorized(
        "/voice/upload?token=secret-token",
        {},
        "secret-token",
    )
    assert stackchan_voice_upload_server.is_upload_authorized(
        "/voice/upload",
        {"Authorization": "Bearer secret-token"},
        "secret-token",
    )
    assert stackchan_voice_upload_server.is_upload_authorized(
        "/voice/upload",
        {"X-Stackchan-Upload-Token": "secret-token"},
        "secret-token",
    )


def test_voice_upload_rate_limiter_limits_by_client():
    limiter = stackchan_voice_upload_server.UploadRateLimiter(limit_per_minute=2)

    assert limiter.allow("192.0.2.1", now=100.0) is True
    assert limiter.allow("192.0.2.1", now=101.0) is True
    assert limiter.allow("192.0.2.1", now=102.0) is False
    assert limiter.allow("192.0.2.2", now=102.0) is True
    assert limiter.allow("192.0.2.1", now=161.1) is True


def test_voice_upload_wake_words_preserve_activation_name():
    matched, prompt_text, wake_word = stackchan_frontend_wake.match_wake_word(
        "小塔，请看看窗外。",
        ("小塔", "机器人"),
    )

    assert matched is True
    assert prompt_text == "小塔，请看看窗外。"
    assert wake_word == "小塔"

    matched, prompt_text, wake_word = stackchan_frontend_wake.match_wake_word(
        "机器人，帮我看看这段。",
        ("小塔", "机器人"),
    )

    assert matched is True
    assert prompt_text == "机器人，帮我看看这段。"
    assert wake_word == "机器人"


def test_voice_upload_wake_words_allow_leading_fillers_and_repeated_first_sound():
    matched, prompt_text, wake_word = stackchan_frontend_wake.match_wake_word(
        "好的，小小塔，晚安。",
        ("小塔", "机器人"),
    )

    assert matched is True
    assert prompt_text == "好的，小小塔，晚安。"
    assert wake_word == "小塔"

    matched, prompt_text, wake_word = stackchan_frontend_wake.match_wake_word(
        "嗯嗯，小塔，继续测试。",
        ("小塔", "机器人"),
    )

    assert matched is True
    assert prompt_text == "嗯嗯，小塔，继续测试。"
    assert wake_word == "小塔"


def test_voice_upload_wake_words_skip_frontend_without_activation(monkeypatch):
    def fake_post(*args, **kwargs):
        raise AssertionError("frontend should not be called without wake word")

    monkeypatch.setattr(stackchan_frontend_wake.requests, "post", fake_post)

    result = stackchan_voice_upload_server.forward_to_frontend(
        {"text": "这只是房间里的声音"},
        wake_url="http://127.0.0.1:3200/wake",
        session_id="117067d6-1111-2222-3333-444444444444",
        wake_words=("小塔", "机器人"),
    )

    assert result["ok"] is False
    assert result["skipped"] == "wake word not found"
    assert result["wake_words"] == ["小塔", "机器人"]
    assert "wake_total" in result["timing_ms"]


def test_voice_upload_frontend_forwarding_posts_wake_request(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True, "session_id": "117067d6-1111-2222-3333-444444444444"}

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(stackchan_frontend_wake.requests, "post", fake_post)

    result = stackchan_voice_upload_server.forward_to_frontend(
        {"text": " 小塔，听得到吗？ "},
        wake_url="http://127.0.0.1:3200/wake",
        session_id="117067d6-1111-2222-3333-444444444444",
        token="agent-token",
        model="claude-opus-4-6[1m]",
        timeout=3,
        wake_words=("小塔", "机器人"),
    )

    assert result["ok"] is True
    assert result["wake_word"] == "小塔"
    assert set(result["timing_ms"]) == {"wake_post", "wake_total"}
    assert calls == [
        {
            "url": "http://127.0.0.1:3200/wake",
            "json": {
                "session_id": "117067d6-1111-2222-3333-444444444444",
                "prompt": "[Stack-chan语音输入] 小塔，听得到吗？",
                "force": True,
                "quiet_minutes": 0,
                "model": "claude-opus-4-6[1m]",
            },
            "headers": {
                "Content-Type": "application/json",
                "Authorization": "Bearer agent-token",
            },
            "timeout": 3,
        }
    ]


def test_voice_upload_frontend_forwarding_retries_busy(monkeypatch):
    calls = []

    class BusyResponse:
        status_code = 409
        text = "busy"

        def json(self):
            raise ValueError

    class OkResponse:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    responses = [BusyResponse(), OkResponse()]

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return responses.pop(0)

    monkeypatch.setattr(stackchan_frontend_wake.requests, "post", fake_post)
    monkeypatch.setattr(stackchan_frontend_wake.time, "sleep", lambda _: None)

    result = stackchan_voice_upload_server.forward_to_frontend(
        {"text": "稍等重试"},
        wake_url="http://127.0.0.1:3200/wake",
        session_id="117067d6-1111-2222-3333-444444444444",
        retries=1,
        retry_delay=0.1,
    )

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert result["body"] == {"ok": True}
    assert set(result["timing_ms"]) == {"wake_post", "wake_total"}
    assert len(calls) == 2


def test_frontend_session_selects_latest_non_archived():
    sessions = [
        {"id": "old", "title": "lab-room-3", "last": "2026-06-20T20:55:19.411Z"},
        {"id": "archived-new", "title": "Test", "last": "2026-06-25T00:00:00Z", "archived": True},
        {"id": "new", "title": "lab-room-4", "last": "2026-06-23T17:35:54.803Z"},
    ]

    selected = stackchan_frontend_session.select_session(sessions)

    assert selected is not None
    assert selected["id"] == "new"


def test_frontend_session_selects_latest_by_title():
    sessions = [
        {"id": "room4-old", "title": "lab-room-4", "last": "2026-06-21T00:00:00Z"},
        {"id": "room3-new", "title": "lab-room-3", "last": "2026-06-23T00:00:00Z"},
        {"id": "room4-new", "title": "lab-room-4 continued", "last": "2026-06-22T00:00:00Z"},
    ]

    selected = stackchan_frontend_session.select_session(sessions, title="lab-room-4")

    assert selected is not None
    assert selected["id"] == "room4-new"


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
    assert set(result["timing_ms"]) == {"pcm_total", "fish_first_chunk", "first_segment_posted"}
    assert request_kwargs["kwargs"]["data"] == struct.pack("<hh", 1000, -1000)
    assert isinstance(request_kwargs["kwargs"]["data"], bytes)
    headers = request_kwargs["kwargs"]["headers"]
    assert headers["Content-Type"].startswith("audio/x-raw")
    assert headers["X-Stackchan-Pcm-Session"] == result["session"]
    assert headers["X-Stackchan-Pcm-Seq"] == "0"
    assert headers["X-Stackchan-Pcm-Final"] == "1"
    assert headers["X-Stackchan-Pcm-Mode"] == "staged"
    assert "final=1" in request_kwargs["args"][0]
    assert "mode=staged" in request_kwargs["args"][0]


def test_post_pcm_stream_handles_split_sample_boundaries(monkeypatch, tmp_path):
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
        iter([b"\xe8", b"\x03\x18", b"\xfc"]),
        tmp_path,
        audio_processing,
    )

    assert result["success"] is True
    assert request_kwargs["kwargs"]["data"] == struct.pack("<hh", 1000, -1000)


def test_post_pcm_stream_staged_segments_do_not_mark_audio_started(monkeypatch, tmp_path):
    calls = []

    class FakeResponse:
        def __init__(self, payload=None, error=None):
            self._payload = payload or {}
            self._error = error
            self.text = "{\"success\":false,\"error\":\"upload failed\"}"

        def raise_for_status(self):
            if self._error:
                error = __import__("requests").HTTPError(self._error)
                error.response = self
                raise error

        def json(self):
            return self._payload

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return FakeResponse({"success": True, "staged": True})
        return FakeResponse(error="500 Server Error")

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", fake_post)
    client = StackchanClient(make_config(pcm_segment_bytes=4))

    with pytest.raises(PcmPlaybackError) as excinfo:
        post_pcm_stream(
            client,
            iter([struct.pack("<hhhh", 1, 2, 3, 4)]),
            tmp_path,
            audio_processing,
        )

    assert excinfo.value.started is False
    assert calls[0][1]["headers"]["X-Stackchan-Pcm-Mode"] == "staged"


def test_wait_for_playback_start_detects_started_ms_change(monkeypatch):
    statuses = iter(
        [
            {"playing": False, "started_ms": 10},
            {"playing": False, "started_ms": 20},
        ]
    )

    class FakeResponse:
        def json(self):
            return next(statuses)

    monkeypatch.setattr("mcp_server.stackchan_client.requests.get", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", lambda _seconds: None)

    result = StackchanClient(make_config()).wait_for_playback_start(
        baseline_started_ms=10,
        timeout=1,
    )

    assert result == {"started": True, "status": {"playing": False, "started_ms": 20}}


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


def test_post_pcm_tcp_stream_sends_header_and_pcm(monkeypatch, tmp_path):
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.response = bytearray(b"OK\n")
            self.timeout = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, data):
            self.sent.append(bytes(data))

        def recv(self, size):
            assert size == 1
            if not self.response:
                return b""
            data = self.response[:1]
            del self.response[:1]
            return bytes(data)

    fake_socket = FakeSocket()
    calls = []

    def fake_create_connection(address, timeout):
        calls.append((address, timeout))
        return fake_socket

    monkeypatch.setattr("mcp_server.stackchan_client.socket.create_connection", fake_create_connection)
    client = StackchanClient(make_config(pcm_stream_initial_buffer_bytes=4))

    result = post_pcm_tcp_stream(
        client,
        iter([struct.pack("<h", 1000), struct.pack("<h", -1000)]),
        tmp_path,
        audio_processing,
    )

    assert result["success"] is True
    assert result["transport"] == "tcp"
    assert result["sent_bytes"] == 4
    assert calls == [(("192.0.2.20", 9090), 3.0)]
    assert fake_socket.sent[0].startswith(b"STACKCHAN_PCM_STREAM/1 session=")
    assert b"rate=24000 channels=1 width=2\n" in fake_socket.sent[0]
    assert fake_socket.sent[1] == struct.pack("<hh", 1000, -1000)


def test_post_pcm_tcp_stream_rejection_is_not_started(monkeypatch, tmp_path):
    class FakeSocket:
        response = bytearray(b"ERR BUSY\n")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            return None

        def sendall(self, _data):
            return None

        def recv(self, _size):
            if not self.response:
                return b""
            data = self.response[:1]
            del self.response[:1]
            return bytes(data)

    monkeypatch.setattr(
        "mcp_server.stackchan_client.socket.create_connection",
        lambda *_args, **_kwargs: FakeSocket(),
    )

    with pytest.raises(PcmPlaybackError, match="ERR BUSY") as excinfo:
        post_pcm_tcp_stream(
            StackchanClient(make_config(pcm_stream_initial_buffer_bytes=2)),
            iter([b"\x00\x00"]),
            tmp_path,
            audio_processing,
        )

    assert excinfo.value.started is False


def test_post_pcm_udp_stream_starts_session_and_sends_framed_packets(monkeypatch, tmp_path):
    class FakeResponse:
        def json(self):
            return {"success": True, "session": "udp-test", "token": 0x12345678, "udp_port": 9091}

    class FakeStatusResponse:
        def json(self):
            return {
                "udp_audio_active": False,
                "udp_audio_playing": False,
                "udp_audio_session": "",
                "udp_last_end_reason": "end",
                "udp_last_frames_received": 1,
                "udp_last_frames_lost": 0,
                "udp_last_underruns": 0,
            }

    class FakeSocket:
        def __init__(self, *_args, **_kwargs):
            self.sent = []
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendto(self, data, address):
            self.sent.append((bytes(data), address))

        def close(self):
            return None

    fake_socket = FakeSocket()
    session_posts = []

    def fake_post(url, **kwargs):
        session_posts.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", fake_post)
    monkeypatch.setattr("mcp_server.stackchan_client.requests.get", lambda *_args, **_kwargs: FakeStatusResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.socket.socket", lambda *_args, **_kwargs: fake_socket)
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", lambda _seconds: None)
    client = StackchanClient(make_config(pcm_transport="udp"))

    result = post_pcm_udp_stream(
        client,
        iter([b"\x00\x00" * (PCM_UDP_FRAME_BYTES // 2)]),
        tmp_path,
        audio_processing,
    )

    assert result["success"] is True
    assert result["transport"] == "udp"
    assert result["frames"] == 1
    assert result["udp_frames_lost"] == 0
    assert result["udp_underruns"] == 0
    assert session_posts[0][0] == "http://192.0.2.20:80/audio/session"
    first_packet, address = fake_socket.sent[0]
    assert address == ("192.0.2.20", 9091)
    magic, version, flags, header_len, token, seq, timestamp, payload_len, reserved = struct.unpack(
        "<4sBBHIIIHH",
        first_packet[:24],
    )
    assert (magic, version, flags, header_len, token, seq, timestamp, payload_len, reserved) == (
        b"SCP1",
        1,
        0,
        24,
        0x12345678,
        0,
        0,
        PCM_UDP_FRAME_BYTES,
        0,
    )
    assert len(first_packet[24:]) == PCM_UDP_FRAME_BYTES
    assert len(fake_socket.sent) == 4
    assert all(packet[0][5] == 1 for packet in fake_socket.sent[1:])


def test_post_pcm_udp_stream_raises_when_device_reports_lost_frames(monkeypatch, tmp_path):
    class FakeSessionResponse:
        def json(self):
            return {"success": True, "session": "udp-test", "token": 0x12345678, "udp_port": 9091}

    class FakeStatusResponse:
        def json(self):
            return {
                "udp_audio_active": False,
                "udp_audio_playing": False,
                "udp_audio_session": "",
                "udp_last_end_reason": "end",
                "udp_last_frames_received": 1,
                "udp_last_frames_lost": 1,
                "udp_last_underruns": 1,
            }

    class FakeSocket:
        def settimeout(self, _timeout):
            return None

        def sendto(self, _data, _address):
            return None

        def close(self):
            return None

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", lambda *_args, **_kwargs: FakeSessionResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.requests.get", lambda *_args, **_kwargs: FakeStatusResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.socket.socket", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", lambda _seconds: None)

    with pytest.raises(PcmPlaybackError, match="lost=1 underruns=1") as excinfo:
        post_pcm_udp_stream(
            StackchanClient(make_config(pcm_transport="udp")),
            iter([b"\x00\x00" * (PCM_UDP_FRAME_BYTES // 2)]),
            tmp_path,
            audio_processing,
        )

    assert excinfo.value.started is True


def test_post_pcm_udp_stream_reports_declicked_samples(monkeypatch, tmp_path):
    class FakeResponse:
        def json(self):
            return {"success": True, "session": "udp-test", "token": 0x12345678, "udp_port": 9091}

    class FakeStatusResponse:
        def json(self):
            return {
                "udp_audio_active": False,
                "udp_audio_playing": False,
                "udp_audio_session": "",
                "udp_last_end_reason": "end",
                "udp_last_frames_received": 1,
                "udp_last_frames_lost": 0,
                "udp_last_underruns": 0,
            }

    class FakeSocket:
        def settimeout(self, _timeout):
            return None

        def sendto(self, _data, _address):
            return None

        def close(self):
            return None

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.requests.get", lambda *_args, **_kwargs: FakeStatusResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.socket.socket", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", lambda _seconds: None)

    result = post_pcm_udp_stream(
        StackchanClient(make_config(pcm_transport="udp")),
        iter([b"\x00\x00" * (PCM_UDP_FRAME_BYTES // 2)]),
        tmp_path,
        audio_processing,
    )

    # The lone frame is both the session's first and last, so it gets both a fade-in and a
    # fade-out ramp counted; on all-zero audio the ramp is a no-op but the sample count is real.
    assert result["declicked_samples"] > 0


def test_post_pcm_udp_stream_raises_when_device_receives_zero_frames(monkeypatch, tmp_path):
    class FakeSessionResponse:
        def json(self):
            return {"success": True, "session": "udp-test", "token": 0x12345678, "udp_port": 9091}

    class FakeStatusResponse:
        def json(self):
            return {
                "udp_audio_active": False,
                "udp_audio_playing": False,
                "udp_audio_session": "",
                "udp_last_end_reason": "end",
                "udp_last_frames_received": 0,
                "udp_last_frames_lost": 0,
                "udp_last_underruns": 0,
            }

    class FakeSocket:
        def settimeout(self, _timeout):
            return None

        def sendto(self, _data, _address):
            return None

        def close(self):
            return None

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", lambda *_args, **_kwargs: FakeSessionResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.requests.get", lambda *_args, **_kwargs: FakeStatusResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.socket.socket", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", lambda _seconds: None)

    with pytest.raises(PcmPlaybackError, match="no frames") as excinfo:
        post_pcm_udp_stream(
            StackchanClient(make_config(pcm_transport="udp")),
            iter([b"\x00\x00" * (PCM_UDP_FRAME_BYTES // 2)]),
            tmp_path,
            audio_processing,
        )

    assert excinfo.value.started is True


def test_post_pcm_udp_stream_raises_when_completion_status_unavailable(monkeypatch, tmp_path):
    class FakeSessionResponse:
        def json(self):
            return {"success": True, "session": "udp-test", "token": 0x12345678, "udp_port": 9091}

    class FakeSocket:
        def settimeout(self, _timeout):
            return None

        def sendto(self, _data, _address):
            return None

        def close(self):
            return None

    def fake_get(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("device unreachable")

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", lambda *_args, **_kwargs: FakeSessionResponse())
    monkeypatch.setattr("mcp_server.stackchan_client.requests.get", fake_get)
    monkeypatch.setattr("mcp_server.stackchan_client.socket.socket", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("mcp_server.stackchan_client.time.sleep", lambda _seconds: None)

    with pytest.raises(PcmPlaybackError, match="could not be verified") as excinfo:
        post_pcm_udp_stream(
            StackchanClient(make_config(pcm_transport="udp")),
            iter([b"\x00\x00" * (PCM_UDP_FRAME_BYTES // 2)]),
            tmp_path,
            audio_processing,
        )

    assert excinfo.value.started is True


def test_post_pcm_udp_stream_session_failure_is_not_started(monkeypatch, tmp_path):
    class FakeResponse:
        def json(self):
            return {"success": False, "error": "busy"}

    monkeypatch.setattr("mcp_server.stackchan_client.requests.post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(PcmPlaybackError, match="PCM UDP session failed") as excinfo:
        post_pcm_udp_stream(
            StackchanClient(make_config()),
            iter([b"\x00\x00"]),
            tmp_path,
            audio_processing,
        )

    assert excinfo.value.started is False


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


def test_health_check_is_non_destructive():
    class FakeClient:
        base_url = "http://192.0.2.20:80"

        def audio_status(self):
            return {"ready": True, "mode": "mcp"}

        def playback_status(self):
            return {"playing": False, "kind": "idle"}

        def get_audio(self):
            raise AssertionError("health check must not consume GET /audio")

    mcp = FakeFastMCP()
    register_tools(mcp, FakeClient(), make_config(), lambda data, format: {"data": data, "format": format})

    report = json.loads(mcp.tools["stackchan_health"]())

    assert report["ok"] is True
    assert report["device"]["audio_status"]["ready"] is True
    assert report["device"]["playback_status"]["playing"] is False
    assert report["config"]["pcm"]["segment_bytes"] == PCM_SEGMENT_BYTES


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
    assert can_stream_pcm(make_config(audio_mode="auto")) is True
    assert can_stream_pcm(make_config(audio_mode="pcm")) is True
    assert can_stream_pcm(make_config(audio_mode="wav")) is False
    assert can_stream_pcm(make_config(tts_engine="edge-tts")) is False
    assert can_stream_pcm(make_config(fish_audio_key="")) is False
