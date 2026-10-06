import json
import time
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from test_mcp_server import FakeFastMCP, make_config

from mcp_server import audio_processing, elevenlabs_tts, mcp_tools
from mcp_server.elevenlabs_tts import TtsSynthesisError
from mcp_server.stackchan_client import PcmPlaybackError


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_OTEL_LOG", str(tmp_path / "otel.jsonl"))
    monkeypatch.setattr(requests, "post", Mock(side_effect=AssertionError("unexpected network request")))
    monkeypatch.setattr(mcp_tools, "start_audio_server", lambda *_args: None)
    monkeypatch.setattr(mcp_tools, "signal_face_tracking", lambda *_args, **_kwargs: None)


def config(**overrides):
    return make_config(**{
        "tts_engine": "elevenlabs",
        "elevenlabs_api_key": "test-eleven-key",
        "elevenlabs_voice_id": "test-voice",
        **overrides,
    })


class Response:
    def __init__(self, chunks=(b"\x01", b"\x00\x02\x00"), status=200, content_type="audio/pcm", error=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("private provider response")

    def iter_content(self, chunk_size):
        assert chunk_size == 4096
        yield from self.chunks
        if self.error:
            raise self.error


def test_request_contract_and_odd_network_chunks(monkeypatch):
    response = Response()
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    cfg = config(elevenlabs_voice_id="voice/segment")
    assert elevenlabs_tts.synthesize_pcm("hello", cfg) == b"\x01\x00\x02\x00"
    args, kwargs = post.call_args
    assert args == ("https://api.elevenlabs.io/v1/text-to-speech/voice%2Fsegment/stream",)
    assert kwargs["params"] == {"output_format": "pcm_24000"}
    assert kwargs["headers"]["xi-api-key"] == cfg.elevenlabs_api_key
    assert cfg.elevenlabs_api_key not in json.dumps(kwargs["json"])
    assert kwargs["json"] == {
        "text": "hello", "model_id": "eleven_v4",
        "voice_settings": {"stability": 0.70, "similarity_boost": 0.75},
    }
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == (10.0, 30.0)
    assert response.closed


@pytest.mark.parametrize("status", [302, 401, 422, 429, 500])
def test_status_failure_is_safe_and_closed(monkeypatch, status):
    response = Response(status=status)
    monkeypatch.setattr(requests, "post", Mock(return_value=response))
    with pytest.raises(TtsSynthesisError, match=f"HTTP {status}"):
        elevenlabs_tts.synthesize_pcm("hello", config())
    assert response.closed


@pytest.mark.parametrize("chunks,content_type,max_bytes", [
    ((), "audio/pcm", 100),
    ((b"\x00",), "audio/pcm", 100),
    ((b"\x00" * 102,), "audio/pcm", 100),
    ((b"{}",), "application/json", 100),
    ((b"audio",), "audio/mpeg", 100),
])
def test_invalid_pcm_is_rejected(monkeypatch, chunks, content_type, max_bytes):
    response = Response(chunks=chunks, content_type=content_type)
    monkeypatch.setattr(requests, "post", Mock(return_value=response))
    with pytest.raises(TtsSynthesisError):
        elevenlabs_tts.synthesize_pcm("hello", config(max_pcm_payload_bytes=max_bytes))
    assert response.closed


def test_synthesis_deadline_checked_after_chunk_and_at_eof():
    for chunks in [(b"\x01\x00",), ()]:
        with pytest.raises(TtsSynthesisError, match="deadline"):
            elevenlabs_tts.collect_pcm(chunks, max_bytes=100, deadline=time.monotonic() - 1)


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError])
def test_partial_elevenlabs_response_falls_back_once_without_leaking(monkeypatch, caplog, error):
    eleven = Response(chunks=(b"\x01\x00",), error=error("private-text test-eleven-key"))
    fish = Response(chunks=(b"\x02\x00",))
    post = Mock(side_effect=[eleven, fish])
    monkeypatch.setattr(requests, "post", post)
    pcm, engine = audio_processing.prepare_elevenlabs_pcm("hello", "en", config())
    assert pcm == b"\x02\x00"
    assert engine == "fish-audio"
    assert post.call_count == 2
    assert post.call_args.kwargs["json"]["reference_id"] == "en-model"
    assert eleven.closed and fish.closed
    assert "private-text" not in caplog.text
    assert "test-eleven-key" not in caplog.text


def test_no_fish_key_reports_failure_without_edge(monkeypatch):
    post = Mock(side_effect=requests.Timeout("secret"))
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(TtsSynthesisError, match="Fish fallback is not configured") as exc:
        audio_processing.prepare_elevenlabs_pcm("hello", "zh", config(fish_audio_key=""))
    assert "secret" not in str(exc.value)
    assert post.call_count == 1


def test_missing_elevenlabs_config_uses_fish_without_network_to_eleven(monkeypatch):
    post = Mock(return_value=Response(chunks=(b"\x02\x00",)))
    monkeypatch.setattr(requests, "post", post)
    assert audio_processing.prepare_elevenlabs_pcm("hello", "zh", config(elevenlabs_api_key="")) == (b"\x02\x00", "fish-audio")
    assert post.call_count == 1
    assert post.call_args.args == ("https://api.fish.audio/v1/tts",)


def test_double_failure_never_calls_body(monkeypatch):
    post = Mock(side_effect=[Response(status=429), Response(error=requests.Timeout("secret"))])
    body = Mock()
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(mcp_tools, "post_pcm_stream", body)
    with pytest.raises(TtsSynthesisError, match="Fish fallback failed"):
        mcp_tools.post_preferred_pcm_stream(Mock(), "hello", "zh", config(transport="relay"))
    body.assert_not_called()
    assert post.call_count == 2


def test_tcp_rejection_reuses_prepared_audio_without_resynthesis(monkeypatch):
    prepare = Mock(return_value=(b"\x01\x00" * 8, "elevenlabs"))
    played = []
    monkeypatch.setattr(audio_processing, "prepare_elevenlabs_pcm", prepare)

    def tcp(_client, chunks, *_args):
        played.append(b"".join(chunks))
        raise PcmPlaybackError("not accepted", started=False)

    def staged(_client, chunks, *_args):
        played.append(b"".join(chunks))
        return {"success": True}

    monkeypatch.setattr(mcp_tools, "post_pcm_tcp_stream", tcp)
    monkeypatch.setattr(mcp_tools, "post_pcm_stream", staged)
    result = mcp_tools.post_preferred_pcm_stream(Mock(), "hello", "zh", config(pcm_transport="auto"))
    assert result["tts_engine"] == "elevenlabs"
    assert result["transport"] == "staged"
    assert played[0] == played[1]
    prepare.assert_called_once()


@pytest.mark.parametrize("engine", ["elevenlabs", "fish-audio"])
def test_relay_reports_actual_provider_without_extra_synthesis(monkeypatch, engine):
    prepare = Mock(return_value=(b"\x01\x00" * 8, engine))
    monkeypatch.setattr(audio_processing, "prepare_elevenlabs_pcm", prepare)
    monkeypatch.setattr(mcp_tools, "post_pcm_stream", Mock(return_value={"success": True}))
    mcp = FakeFastMCP()
    mcp_tools.register_tools(mcp, SimpleNamespace(), config(transport="relay"), Mock())
    confirmation = mcp.tools["stackchan_say"]("hello")
    assert "Stack-chan is saying" in confirmation
    assert ("Fish fallback" in confirmation) is (engine == "fish-audio")
    prepare.assert_called_once()


@pytest.mark.parametrize("error", [PcmPlaybackError("started", started=True), RuntimeError("unknown outcome")])
def test_device_failure_does_not_repeat_tts_as_wav(monkeypatch, error):
    prepare = Mock(return_value=(b"\x01\x00", "elevenlabs"))
    wav = Mock()
    monkeypatch.setattr(audio_processing, "prepare_elevenlabs_pcm", prepare)
    monkeypatch.setattr(audio_processing, "generate_tts", wav)
    monkeypatch.setattr(mcp_tools, "post_pcm_tcp_stream", Mock(side_effect=error))
    mcp = FakeFastMCP()
    mcp_tools.register_tools(mcp, SimpleNamespace(), config(), Mock())
    assert "failed" in mcp.tools["stackchan_say"]("hello")
    prepare.assert_called_once()
    wav.assert_not_called()


def test_wav_mode_wraps_the_same_pcm(monkeypatch, tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(audio_processing, "TEMP_AUDIO_DIR", temp)
    monkeypatch.setattr(audio_processing, "AUDIO_DIR", tmp_path)
    pcm = b"\x01\x00" * 100
    monkeypatch.setattr(audio_processing, "prepare_elevenlabs_pcm", Mock(return_value=(pcm, "elevenlabs")))
    path = audio_processing.generate_tts("hello", "zh", config(audio_mode="wav"))
    audio_processing.validate_playback_wav(path)
    with wave.open(str(path), "rb") as wav:
        assert wav.readframes(100) == pcm
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)


@pytest.mark.parametrize("settings,expected", [
    ({}, True), ({"audio_mode": "wav"}, False),
    ({"elevenlabs_api_key": "", "fish_audio_key": ""}, False),
    ({"elevenlabs_voice_id": "", "fish_audio_key": "test-key"}, True),
])
def test_pcm_engine_selection(settings, expected):
    assert mcp_tools.can_stream_pcm(config(**settings)) is expected
