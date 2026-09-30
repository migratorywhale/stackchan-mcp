import logging
import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_dotenv(path: Path | None = None) -> None:
    """Load project .env so launchd and direct shell starts share one config."""
    env_path = path or (Path(__file__).resolve().parents[1] / ".env")
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(errors="ignore").splitlines()
    except OSError as exc:
        logger.warning("Could not read %s: %s", env_path, exc)
        return

    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip().strip("'\"")
        os.environ[key] = value


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%s; using %.2f", name, raw, default)
        return default


def env_float_any(names: tuple[str, ...], default: float) -> float:
    for name in names:
        if name in os.environ:
            return env_float(name, default)
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


def env_int_any(names: tuple[str, ...], default: int) -> int:
    for name in names:
        if name in os.environ:
            return env_int(name, default)
    return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class StackchanConfig:
    stackchan_ip: str
    stackchan_port: int
    mac_ip: str
    audio_serve_port: int
    tts_engine: str
    audio_mode: str
    pcm_transport: str
    save_pcm: bool
    pcm_gain: float
    pcm_limit: float
    pcm_segment_bytes: int
    pcm_stream_port: int
    pcm_stream_initial_buffer_bytes: int
    pcm_stream_connect_timeout: float
    pcm_stream_io_timeout: float
    udp_pace_factor: float
    max_pcm_payload_bytes: int
    pcm_declick_samples: int
    pcm_zero_cross_window: int
    pcm_first_segment_timeout: float
    http_play_timeout: float
    http_audio_timeout: float
    http_status_timeout: float
    http_command_timeout: float
    http_snapshot_warmup_timeout: float
    http_snapshot_timeout: float
    playback_start_timeout: float
    playback_poll_interval: float
    pcm_segment_post_timeout: float
    fish_tts_timeout: float
    fish_asr_timeout: float
    fish_stream_chunk_bytes: int
    edge_tts_bin: str
    fish_audio_key: str
    fish_audio_model_zh: str
    fish_audio_model_en: str
    mcp_auth_token: str = ""
    audio_publish_target: str = ""
    audio_publish_timeout: float = 20.0
    audio_publish_attempts: int = 2
    http_probe_timeout: float = 8.0
    transport: str = "direct"
    relay_url: str = "http://127.0.0.1:8766"
    relay_token: str = field(default="", repr=False)


VALID_AUDIO_MODES = {"auto", "pcm", "wav"}
VALID_PCM_TRANSPORTS = {"auto", "udp", "tcp", "staged"}
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_CONTENT_TYPE = "audio/x-raw;format=s16le;rate=24000;channels=1"
MAX_PCM_PAYLOAD_BYTES = 2 * 1024 * 1024
PCM_SEGMENT_BYTES = 48 * 1024
PCM_STREAM_PORT = 9090
PCM_UDP_PORT = 9091
PCM_UDP_FRAME_MS = 10
PCM_UDP_FRAME_BYTES = (PCM_SAMPLE_RATE * PCM_UDP_FRAME_MS // 1000) * PCM_SAMPLE_WIDTH
PCM_STREAM_INITIAL_BUFFER_BYTES = 96 * 1024
FISH_STREAM_CHUNK_BYTES = 4096
DEFAULT_AUDIO_DIR = str(Path(tempfile.gettempdir()) / "stackchan_audio")
VALID_FACES = ("calm", "thinking", "happy", "sleepy", "shy", "smug", "pouty")
EDGE_VOICES = {
    "zh": "zh-CN-YunxiNeural",
    "en": "en-US-GuyNeural",
}


def config_summary(config: StackchanConfig) -> dict[str, Any]:
    return {
        "stackchan": {
            "transport": config.transport,
            "ip": config.stackchan_ip,
            "port": config.stackchan_port,
            "base_url": (
                f"{config.relay_url.rstrip('/')}/device"
                if config.transport == "relay"
                else f"http://{config.stackchan_ip}:{config.stackchan_port}"
            ),
            "relay_token_configured": bool(config.relay_token),
        },
        "audio": {
            "mac_ip": config.mac_ip,
            "serve_port": config.audio_serve_port,
            "mode": config.audio_mode,
            "save_pcm": config.save_pcm,
            "publish_configured": bool(config.audio_publish_target),
            "publish_timeout": config.audio_publish_timeout,
            "publish_attempts": config.audio_publish_attempts,
        },
        "pcm": {
            "transport": config.pcm_transport,
            "sample_rate": PCM_SAMPLE_RATE,
            "channels": PCM_CHANNELS,
            "sample_width": PCM_SAMPLE_WIDTH,
            "segment_bytes": config.pcm_segment_bytes,
            "stream_port": config.pcm_stream_port,
            "udp_port": PCM_UDP_PORT,
            "udp_frame_ms": PCM_UDP_FRAME_MS,
            "udp_frame_bytes": PCM_UDP_FRAME_BYTES,
            "udp_pace_factor": config.udp_pace_factor,
            "stream_initial_buffer_bytes": config.pcm_stream_initial_buffer_bytes,
            "max_payload_bytes": config.max_pcm_payload_bytes,
            "gain": config.pcm_gain,
            "limit": config.pcm_limit,
            "declick_samples": config.pcm_declick_samples,
            "zero_cross_window": config.pcm_zero_cross_window,
            "first_segment_timeout": config.pcm_first_segment_timeout,
            "stream_connect_timeout": config.pcm_stream_connect_timeout,
            "stream_io_timeout": config.pcm_stream_io_timeout,
        },
        "tts": {
            "engine": config.tts_engine,
            "edge_tts_bin": config.edge_tts_bin,
            "fish_audio_key_configured": bool(config.fish_audio_key),
            "fish_audio_model_zh_configured": bool(config.fish_audio_model_zh),
            "fish_audio_model_en_configured": bool(config.fish_audio_model_en),
            "fish_stream_chunk_bytes": config.fish_stream_chunk_bytes,
        },
        "mcp_auth_token_configured": bool(config.mcp_auth_token),
        "timeouts": {
            "http_play": config.http_play_timeout,
            "http_audio": config.http_audio_timeout,
            "http_status": config.http_status_timeout,
            "http_probe": config.http_probe_timeout,
            "http_command": config.http_command_timeout,
            "http_snapshot_warmup": config.http_snapshot_warmup_timeout,
            "http_snapshot": config.http_snapshot_timeout,
            "playback_start": config.playback_start_timeout,
            "playback_poll_interval": config.playback_poll_interval,
            "pcm_segment_post": config.pcm_segment_post_timeout,
            "fish_tts": config.fish_tts_timeout,
            "fish_asr": config.fish_asr_timeout,
        },
    }



def _local_ip_toward(host: str, port: int = 80) -> str | None:
    """Return the local interface IP that routes to host:port (no packets sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect((host, port))
            ip = s.getsockname()[0]
            return ip if ip and not ip.startswith("0.") else None
    except OSError:
        return None


def resolve_mac_ip(value: str | None, stackchan_ip: str, stackchan_port: int = 80) -> str:
    """Resolve MAC_IP for audio URLs the device fetches from this host.

    - "auto" / empty → detect the interface that routes to the device
      (2026/9/22: router re-leased the host a new address overnight and the
      device kept fetching from a dead address; explicit IPs go stale).
    - explicit → kept as-is (relocate/travel mode needs it), but warn when it is
      not the interface that routes to the device.
    """
    raw = (value or "").strip()
    detected = _local_ip_toward(stackchan_ip, stackchan_port)
    if raw == "" or raw.lower() == "auto":
        if detected:
            return detected
        logger.warning("MAC_IP=auto but could not detect a route to %s; using 127.0.0.1", stackchan_ip)
        return "127.0.0.1"
    if detected and detected != raw and raw != "127.0.0.1":
        logger.warning(
            "MAC_IP=%s but the interface routing to %s is %s; device audio fetch may fail (set MAC_IP=auto)",
            raw, stackchan_ip, detected,
        )
    return raw

def load_config() -> StackchanConfig:
    load_dotenv()

    transport = os.environ.get("STACKCHAN_TRANSPORT", "direct").strip().lower()
    if transport not in {"direct", "relay"}:
        raise ValueError("STACKCHAN_TRANSPORT must be direct or relay")
    relay_token = os.environ.get("STACKCHAN_RELAY_TOKEN", "")
    if transport == "relay" and (token_file := os.environ.get("STACKCHAN_RELAY_TOKEN_FILE")):
        try:
            relay_token = Path(token_file).read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("Could not read STACKCHAN_RELAY_TOKEN_FILE") from exc

    audio_mode = os.environ.get("STACKCHAN_AUDIO_MODE", "wav").lower()
    if audio_mode not in VALID_AUDIO_MODES:
        logger.warning("Invalid STACKCHAN_AUDIO_MODE=%s; using wav", audio_mode)
        audio_mode = "wav"
    pcm_transport = os.environ.get("STACKCHAN_PCM_TRANSPORT", os.environ.get("STACKCHAN_AUDIO_TRANSPORT", "tcp")).lower()
    if pcm_transport not in VALID_PCM_TRANSPORTS:
        logger.warning("Invalid STACKCHAN_PCM_TRANSPORT=%s; using auto", pcm_transport)
        pcm_transport = "auto"

    pcm_segment_bytes = clamp_int(
        env_int("STACKCHAN_PCM_SEGMENT_BYTES", PCM_SEGMENT_BYTES),
        PCM_SAMPLE_WIDTH,
        MAX_PCM_PAYLOAD_BYTES,
    )
    pcm_segment_bytes -= pcm_segment_bytes % PCM_SAMPLE_WIDTH
    max_pcm_payload_bytes = clamp_int(
        env_int_any(
            ("STACKCHAN_PCM_MAX_PAYLOAD_BYTES", "STACKCHAN_MAX_PCM_PAYLOAD_BYTES"),
            MAX_PCM_PAYLOAD_BYTES,
        ),
        pcm_segment_bytes,
        64 * 1024 * 1024,
    )
    pcm_gain = max(0.0, min(env_float("STACKCHAN_PCM_GAIN", 1.0), 1.0))
    pcm_limit = max(0.1, min(env_float("STACKCHAN_PCM_LIMIT", 0.90), 1.0))
    pcm_declick_samples = max(
        0,
        min(env_int("STACKCHAN_PCM_DECLICK_SAMPLES", 64), pcm_segment_bytes // PCM_SAMPLE_WIDTH),
    )
    pcm_zero_cross_window = max(
        0,
        min(env_int("STACKCHAN_PCM_ZERO_CROSS_WINDOW", 256), pcm_segment_bytes // PCM_SAMPLE_WIDTH),
    )

    return StackchanConfig(
        stackchan_ip=os.environ.get("STACKCHAN_IP", "127.0.0.1"),
        stackchan_port=int(os.environ.get("STACKCHAN_PORT", 80)),
        mac_ip=resolve_mac_ip(
            os.environ.get("MAC_IP", "auto"),
            os.environ.get("STACKCHAN_IP", "127.0.0.1"),
            int(os.environ.get("STACKCHAN_PORT", 80)),
        ) if transport == "direct" else "127.0.0.1",
        audio_serve_port=int(os.environ.get("AUDIO_SERVE_PORT", 5060)),
        tts_engine=os.environ.get("TTS_ENGINE", "fish-audio"),
        audio_mode=audio_mode,
        pcm_transport=pcm_transport,
        save_pcm=env_bool("STACKCHAN_SAVE_PCM"),
        pcm_gain=pcm_gain,
        pcm_limit=pcm_limit,
        pcm_segment_bytes=pcm_segment_bytes,
        pcm_stream_port=clamp_int(env_int("STACKCHAN_PCM_STREAM_PORT", PCM_STREAM_PORT), 1, 65535),
        pcm_stream_initial_buffer_bytes=clamp_int(
            env_int("STACKCHAN_PCM_STREAM_INITIAL_BUFFER_BYTES", PCM_STREAM_INITIAL_BUFFER_BYTES),
            PCM_SAMPLE_WIDTH,
            MAX_PCM_PAYLOAD_BYTES,
        ),
        pcm_stream_connect_timeout=env_float_any(
            ("STACKCHAN_PCM_STREAM_CONNECT_TIMEOUT", "STACKCHAN_PCM_STREAM_CONNECT_TIMEOUT_SEC"),
            3.0,
        ),
        pcm_stream_io_timeout=env_float_any(
            ("STACKCHAN_PCM_STREAM_IO_TIMEOUT", "STACKCHAN_PCM_STREAM_IO_TIMEOUT_SEC"),
            30.0,
        ),
        udp_pace_factor=max(0.85, min(env_float("STACKCHAN_UDP_PACE_FACTOR", 1.0), 1.5)),
        max_pcm_payload_bytes=max_pcm_payload_bytes,
        pcm_declick_samples=pcm_declick_samples,
        pcm_zero_cross_window=pcm_zero_cross_window,
        pcm_first_segment_timeout=env_float_any(
            ("STACKCHAN_PCM_FIRST_SEGMENT_TIMEOUT", "STACKCHAN_PCM_FIRST_SEGMENT_TIMEOUT_SEC"),
            3.0,
        ),
        http_play_timeout=env_float_any(("STACKCHAN_HTTP_PLAY_TIMEOUT", "STACKCHAN_HTTP_PLAY_TIMEOUT_SEC"), 5.0),
        http_audio_timeout=env_float_any(("STACKCHAN_HTTP_AUDIO_TIMEOUT", "STACKCHAN_HTTP_AUDIO_TIMEOUT_SEC"), 10.0),
        http_status_timeout=env_float_any(("STACKCHAN_HTTP_STATUS_TIMEOUT", "STACKCHAN_HTTP_STATUS_TIMEOUT_SEC"), 3.0),
        http_probe_timeout=max(1.0, min(env_float("STACKCHAN_HTTP_PROBE_TIMEOUT", 8.0), 30.0)),
        http_command_timeout=env_float_any(("STACKCHAN_HTTP_COMMAND_TIMEOUT", "STACKCHAN_HTTP_COMMAND_TIMEOUT_SEC"), 5.0),
        http_snapshot_warmup_timeout=env_float_any(
            ("STACKCHAN_HTTP_SNAPSHOT_WARMUP_TIMEOUT", "STACKCHAN_HTTP_SNAPSHOT_WARMUP_TIMEOUT_SEC"),
            5.0,
        ),
        http_snapshot_timeout=env_float_any(
            ("STACKCHAN_HTTP_SNAPSHOT_TIMEOUT", "STACKCHAN_HTTP_SNAPSHOT_TIMEOUT_SEC"),
            10.0,
        ),
        playback_start_timeout=env_float_any(
            ("STACKCHAN_PLAYBACK_START_TIMEOUT", "STACKCHAN_PLAYBACK_START_TIMEOUT_SEC"),
            5.0,
        ),
        playback_poll_interval=env_float_any(
            ("STACKCHAN_PLAYBACK_POLL_INTERVAL", "STACKCHAN_PLAYBACK_POLL_INTERVAL_SEC"),
            0.2,
        ),
        pcm_segment_post_timeout=env_float_any(
            ("STACKCHAN_PCM_SEGMENT_POST_TIMEOUT", "STACKCHAN_PCM_SEGMENT_POST_TIMEOUT_SEC"),
            30.0,
        ),
        fish_tts_timeout=env_float_any(("STACKCHAN_FISH_TTS_TIMEOUT", "STACKCHAN_FISH_TTS_TIMEOUT_SEC"), 30.0),
        fish_asr_timeout=env_float_any(("STACKCHAN_FISH_ASR_TIMEOUT", "STACKCHAN_FISH_ASR_TIMEOUT_SEC"), 15.0),
        fish_stream_chunk_bytes=clamp_int(
            env_int("STACKCHAN_FISH_STREAM_CHUNK_BYTES", FISH_STREAM_CHUNK_BYTES),
            PCM_SAMPLE_WIDTH,
            1024 * 1024,
        ),
        edge_tts_bin=os.environ.get("EDGE_TTS_BIN", "edge-tts"),
        fish_audio_key=os.environ.get("FISH_AUDIO_KEY", ""),
        fish_audio_model_zh=os.environ.get("FISH_AUDIO_MODEL_ZH", ""),
        fish_audio_model_en=os.environ.get("FISH_AUDIO_MODEL_EN", ""),
        mcp_auth_token=os.environ.get("STACKCHAN_MCP_AUTH_TOKEN", ""),
        audio_publish_target=os.environ.get("STACKCHAN_AUDIO_PUBLISH_TARGET", ""),
        audio_publish_timeout=env_float_any(
            ("STACKCHAN_AUDIO_PUBLISH_TIMEOUT", "STACKCHAN_AUDIO_PUBLISH_TIMEOUT_SEC"),
            20.0,
        ),
        audio_publish_attempts=clamp_int(
            env_int("STACKCHAN_AUDIO_PUBLISH_ATTEMPTS", 2),
            1,
            3,
        ),
        transport=transport,
        relay_url=os.environ.get("STACKCHAN_RELAY_URL", "http://127.0.0.1:8766").rstrip("/"),
        relay_token=relay_token,
    )
