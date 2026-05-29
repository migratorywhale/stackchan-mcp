import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %.2f", name, raw, default)
        return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %d", name, raw, default)
        return default


@dataclass(frozen=True)
class StackchanConfig:
    stackchan_ip: str
    stackchan_port: int
    mac_ip: str
    audio_serve_port: int
    tts_engine: str
    audio_mode: str
    save_pcm: bool
    pcm_gain: float
    pcm_limit: float
    pcm_declick_samples: int
    pcm_zero_cross_window: int
    edge_tts_bin: str
    fish_audio_key: str
    fish_audio_model_zh: str
    fish_audio_model_en: str


VALID_AUDIO_MODES = {"auto", "pcm", "wav"}
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_CONTENT_TYPE = "audio/x-raw;format=s16le;rate=24000;channels=1"
MAX_PCM_PAYLOAD_BYTES = 2 * 1024 * 1024
PCM_SEGMENT_BYTES = 48 * 1024
VALID_FACES = ("calm", "thinking", "happy", "sleepy", "shy", "smug", "pouty")
EDGE_VOICES = {
    "zh": "zh-CN-YunxiNeural",
    "en": "en-US-GuyNeural",
}


def load_config() -> StackchanConfig:
    audio_mode = os.environ.get("STACKCHAN_AUDIO_MODE", "auto").lower()
    if audio_mode not in VALID_AUDIO_MODES:
        logger.warning("Invalid STACKCHAN_AUDIO_MODE=%s; using auto", audio_mode)
        audio_mode = "auto"

    pcm_gain = max(0.0, min(env_float("STACKCHAN_PCM_GAIN", 0.75), 1.0))
    pcm_limit = max(0.1, min(env_float("STACKCHAN_PCM_LIMIT", 0.90), 1.0))
    pcm_declick_samples = max(
        0,
        min(env_int("STACKCHAN_PCM_DECLICK_SAMPLES", 64), PCM_SEGMENT_BYTES // PCM_SAMPLE_WIDTH),
    )
    pcm_zero_cross_window = max(
        0,
        min(env_int("STACKCHAN_PCM_ZERO_CROSS_WINDOW", 256), PCM_SEGMENT_BYTES // PCM_SAMPLE_WIDTH),
    )

    return StackchanConfig(
        stackchan_ip=os.environ.get("STACKCHAN_IP", "10.83.20.187"),
        stackchan_port=int(os.environ.get("STACKCHAN_PORT", 80)),
        mac_ip=os.environ.get("MAC_IP", "10.83.20.149"),
        audio_serve_port=int(os.environ.get("AUDIO_SERVE_PORT", 5060)),
        tts_engine=os.environ.get("TTS_ENGINE", "fish-audio"),
        audio_mode=audio_mode,
        save_pcm=os.environ.get("STACKCHAN_SAVE_PCM", "0").lower() in {"1", "true", "yes"},
        pcm_gain=pcm_gain,
        pcm_limit=pcm_limit,
        pcm_declick_samples=pcm_declick_samples,
        pcm_zero_cross_window=pcm_zero_cross_window,
        edge_tts_bin=os.environ.get("EDGE_TTS_BIN", "/Users/Isa/Kokoro-TTS-Local/venv/bin/edge-tts"),
        fish_audio_key=os.environ.get("FISH_AUDIO_KEY", ""),
        fish_audio_model_zh=os.environ.get("FISH_AUDIO_MODEL_ZH", "411d04608a3a498192e16724689e7993"),
        fish_audio_model_en=os.environ.get("FISH_AUDIO_MODEL_EN", "a1e3e14176b0496c84e6009d672c23f8"),
    )
