import json
import os
from dataclasses import fields

import pytest

from mcp_server import stackchan_config


@pytest.fixture(autouse=True)
def isolated_config_env(monkeypatch):
    for name in os.environ:
        if name.startswith(("STACKCHAN_", "FISH_AUDIO_", "ELEVENLABS_")) or name in {
            "MAC_IP", "TTS_ENGINE", "AUDIO_SERVE_PORT", "EDGE_TTS_BIN"
        }:
            monkeypatch.delenv(name)
    monkeypatch.setattr(stackchan_config, "load_dotenv", lambda: None)
    monkeypatch.setattr(stackchan_config, "resolve_mac_ip", lambda *_args: "192.0.2.10")


def assert_elevenlabs_defaults(config):
    assert config.elevenlabs_api_key == ""
    assert config.elevenlabs_voice_id == ""
    assert config.elevenlabs_model_id == "eleven_v4"
    assert config.elevenlabs_stability == 0.70
    assert config.elevenlabs_similarity == 0.75
    assert config.elevenlabs_tts_timeout == 30.0


def test_elevenlabs_defaults_preserve_fish_engine():
    config = stackchan_config.load_config()

    assert config.tts_engine == "fish-audio"
    assert_elevenlabs_defaults(config)


@pytest.mark.parametrize("positional", [False, True])
def test_existing_dataclass_constructors_need_no_elevenlabs_fields(positional):
    loaded = stackchan_config.load_config()
    previous_fields = {
        item.name: getattr(loaded, item.name)
        for item in fields(loaded)
        if not item.name.startswith("elevenlabs_")
    }

    if positional:
        config = stackchan_config.StackchanConfig(*previous_fields.values())
    else:
        config = stackchan_config.StackchanConfig(**previous_fields)

    assert config == loaded
    assert_elevenlabs_defaults(config)


def test_elevenlabs_env_settings_and_fish_fallback_config(monkeypatch):
    settings = {
        "TTS_ENGINE": "elevenlabs",
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "ELEVENLABS_VOICE_ID": "test-elevenlabs-voice",
        "ELEVENLABS_MODEL_ID": "custom-model",
        "ELEVENLABS_STABILITY": "0.45",
        "ELEVENLABS_SIMILARITY": "0.85",
        "STACKCHAN_ELEVENLABS_TTS_TIMEOUT": "12.5",
        "FISH_AUDIO_KEY": "test-fish-key",
        "FISH_AUDIO_MODEL_ZH": "test-fish-zh",
        "FISH_AUDIO_MODEL_EN": "test-fish-en",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    config = stackchan_config.load_config()

    assert config.tts_engine == "elevenlabs"
    assert config.elevenlabs_api_key == "test-elevenlabs-key"
    assert config.elevenlabs_voice_id == "test-elevenlabs-voice"
    assert config.elevenlabs_model_id == "custom-model"
    assert config.elevenlabs_stability == 0.45
    assert config.elevenlabs_similarity == 0.85
    assert config.elevenlabs_tts_timeout == 12.5
    assert config.fish_audio_key == "test-fish-key"
    assert config.fish_audio_model_zh == "test-fish-zh"
    assert config.fish_audio_model_en == "test-fish-en"

    summary = stackchan_config.config_summary(config)
    assert summary["tts"]["engine"] == "elevenlabs"
    assert summary["tts"]["elevenlabs_model_id"] == "custom-model"
    assert summary["tts"]["elevenlabs_stability"] == 0.45
    assert summary["tts"]["elevenlabs_similarity"] == 0.85
    assert summary["timeouts"]["elevenlabs_tts"] == 12.5


@pytest.mark.parametrize("setting", ["stability", "similarity"])
@pytest.mark.parametrize("value", ["0", "1", "0.125", " 0.8 "])
def test_elevenlabs_unit_settings_accept_valid_values(monkeypatch, setting, value):
    monkeypatch.setenv(f"ELEVENLABS_{setting.upper()}", value)

    config = stackchan_config.load_config()

    assert getattr(config, f"elevenlabs_{setting}") == float(value)


@pytest.mark.parametrize("setting,default", [("stability", 0.70), ("similarity", 0.75)])
@pytest.mark.parametrize("value", ["", "invalid", "nan", "inf", "-inf", "1e999", "-0.1", "1.1"])
def test_elevenlabs_invalid_unit_settings_use_defaults(monkeypatch, caplog, setting, default, value):
    name = f"ELEVENLABS_{setting.upper()}"
    monkeypatch.setenv(name, value)

    config = stackchan_config.load_config()

    assert getattr(config, f"elevenlabs_{setting}") == default
    assert name in caplog.text


@pytest.mark.parametrize("value", ["0.001", "1", "120.5", " 3e1 "])
def test_elevenlabs_timeout_accepts_finite_positive_values(monkeypatch, value):
    monkeypatch.setenv("STACKCHAN_ELEVENLABS_TTS_TIMEOUT", value)

    assert stackchan_config.load_config().elevenlabs_tts_timeout == float(value)


@pytest.mark.parametrize("value", ["", "invalid", "nan", "inf", "-inf", "1e999", "0", "-0.0", "-1"])
def test_elevenlabs_invalid_timeout_uses_default(monkeypatch, caplog, value):
    monkeypatch.setenv("STACKCHAN_ELEVENLABS_TTS_TIMEOUT", value)

    assert stackchan_config.load_config().elevenlabs_tts_timeout == 30.0
    assert "STACKCHAN_ELEVENLABS_TTS_TIMEOUT" in caplog.text


@pytest.mark.parametrize("key_configured", [False, True])
@pytest.mark.parametrize("voice_configured", [False, True])
def test_elevenlabs_diagnostics_hide_key_and_voice(monkeypatch, caplog, key_configured, voice_configured):
    key = "private-elevenlabs-key"
    voice = "private-elevenlabs-voice"
    monkeypatch.setenv("ELEVENLABS_API_KEY", key if key_configured else "")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", voice if voice_configured else "")

    config = stackchan_config.load_config()
    summary = stackchan_config.config_summary(config)

    assert summary["tts"]["elevenlabs_api_key_configured"] is key_configured
    assert summary["tts"]["elevenlabs_voice_id_configured"] is voice_configured
    assert "elevenlabs_api_key" not in summary["tts"]
    assert "elevenlabs_voice_id" not in summary["tts"]
    for diagnostic in (json.dumps(summary), repr(config), caplog.text):
        assert key not in diagnostic
        assert voice not in diagnostic
