import logging
import time

import requests

from . import audio_processing
from .audio_server import AUDIO_DIR, audio_url, start_audio_server
from .stackchan_client import PcmPlaybackError, StackchanClient, post_pcm_stream
from .stackchan_config import VALID_FACES, StackchanConfig

logger = logging.getLogger(__name__)


def can_stream_pcm(config: StackchanConfig) -> bool:
    return (
        config.audio_mode != "wav"
        and config.tts_engine == "fish-audio"
        and bool(config.fish_audio_key)
    )


def register_tools(mcp, client: StackchanClient, config: StackchanConfig, image_cls):
    @mcp.tool()
    def stackchan_say(text: str, lang: str = "zh") -> str:
        start_audio_server(config.audio_serve_port)

        try:
            pcm_fallback_reason = None
            if can_stream_pcm(config):
                try:
                    result = post_pcm_stream(
                        client,
                        audio_processing.iter_fish_pcm_stream(text, lang, config),
                        AUDIO_DIR,
                        audio_processing,
                    )
                    if result.get("success"):
                        diag = (
                            f" session={result.get('session', '?')}"
                            f" segments={result.get('segments', '?')}"
                            f" bytes={result.get('total_bytes', '?')}"
                            f" gain={result.get('pcm_gain', '?')}"
                            f" limited={result.get('limited_samples', '?')}"
                            f" declicked={result.get('declicked_samples', '?')}"
                        )
                        if result.get("saved_pcm"):
                            diag += f" saved={result['saved_pcm']}"
                        return f"🗣️ Stack-chan is saying: \"{text[:60]}{'…' if len(text)>60 else ''}\" [Fish Audio PCM/{lang}{diag}]"
                    pcm_fallback_reason = f"PCM play returned {result}"
                    if config.audio_mode == "pcm":
                        return f"❌ PCM play failed: {result}"
                    logger.warning("Falling back to WAV TTS: %s", pcm_fallback_reason)
                except PcmPlaybackError as exc:
                    if exc.started:
                        logger.error("PCM playback failed after audio started: %s", exc)
                        return f"❌ PCM playback failed after audio started: {exc}"
                    pcm_fallback_reason = str(exc)
                    if config.audio_mode == "pcm":
                        logger.error("PCM playback failed in forced PCM mode: %s", exc)
                        return f"❌ PCM playback failed: {exc}"
                    logger.warning("Falling back to WAV TTS after PCM failure: %s", exc)
                except Exception as exc:
                    pcm_fallback_reason = str(exc)
                    if config.audio_mode == "pcm":
                        logger.error("PCM playback failed in forced PCM mode: %s", exc)
                        return f"❌ PCM playback failed: {exc}"
                    logger.warning("Falling back to WAV TTS after PCM failure: %s", exc)
            elif config.audio_mode == "pcm":
                return "❌ PCM playback unavailable: TTS_ENGINE must be fish-audio and FISH_AUDIO_KEY must be set"

            wav_path = audio_processing.generate_tts(text, lang, config)
            audio_processing.validate_playback_wav(wav_path)
            result = client.play(audio_url(config.mac_ip, config.audio_serve_port, wav_path.name))

            if result.get("success"):
                engine = "Fish Audio" if (config.tts_engine == "fish-audio" and config.fish_audio_key) else "edge-tts"
                fallback_note = " (PCM fallback)" if pcm_fallback_reason else ""
                return f"🗣️ Stack-chan is saying: \"{text[:60]}{'…' if len(text)>60 else ''}\" [{engine} WAV/{lang} mode={config.audio_mode}]{fallback_note}"
            return f"❌ Play failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_listen(lang: str = "zh") -> str:
        try:
            status = client.audio_status()
            if not status.get("ready"):
                return "🎤 No recording ready. Stack-chan is listening... (speak to it and try again)"

            audio_data = client.get_audio()
            if audio_data is None:
                return "❌ Failed to fetch audio from Stack-chan"

            wav_path = AUDIO_DIR / f"rec_{int(time.time()*1000)}.wav"
            wav_path.write_bytes(audio_data)
            asr_result = audio_processing.transcribe_audio(wav_path, lang, config)
            text = asr_result.get("text", "")
            asr_duration = asr_result.get("duration", 0)
            asr_lang = asr_result.get("language", "?")
            if text:
                return f"👂 Heard ({asr_duration:.1f}s, {asr_lang}): \"{text}\""
            return f"🎤 Recording captured ({len(audio_data)} bytes, {asr_duration:.1f}s) but ASR returned empty text. Detected language: {asr_lang}. Audio may be too quiet."
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_move(x: float = 0, y: float = 0, speed: int = 50) -> str:
        try:
            x = max(-128, min(128, x))
            y = max(0, min(90, y))
            speed = max(0, min(100, speed))
            result = client.move(x, y, speed)
            if result.get("success"):
                return f"🤖 Head moved to x={x:.0f}° y={y:.0f}° (speed {speed}%)"
            return f"❌ Move failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_nod() -> str:
        try:
            result = client.gesture("nod")
            return "🤖 *nods yes*" if result.get("success") else f"❌ Nod failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_shake() -> str:
        try:
            result = client.gesture("shake")
            return "🤖 *shakes head no*" if result.get("success") else f"❌ Shake failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_face(expression: str = "calm") -> str:
        if expression not in VALID_FACES:
            return f"❌ Unknown expression. Choose from: {', '.join(VALID_FACES)}"
        try:
            result = client.set_face(expression)
            if result.get("success"):
                faces = {
                    "calm": "😊",
                    "thinking": "🤔",
                    "happy": "🐋",
                    "sleepy": "😴",
                    "shy": "😳",
                    "smug": "😏",
                    "pouty": "😤",
                }
                return f"{faces.get(expression, '🤖')} Face: {expression}"
            return f"❌ Face change failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_see() -> list:
        try:
            jpeg_data, size = client.snapshot()
            if jpeg_data is None:
                return "❌ Camera capture failed"
            img_path = AUDIO_DIR / f"cam_{int(time.time()*1000)}.jpg"
            img_path.write_bytes(jpeg_data)
            return [
                image_cls(data=jpeg_data, format="jpeg"),
                f"📷 Photo captured ({size} bytes). Saved to: {img_path}",
            ]
        except requests.exceptions.ConnectionError:
            return f"❌ Stack-chan offline (cannot reach {config.stackchan_ip})"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_home() -> str:
        try:
            result = client.gesture("home")
            return "🤖 Head returned to home position" if result.get("success") else f"❌ Home failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_status() -> str:
        try:
            status = client.audio_status()
            return f"✅ Stack-chan online at {config.stackchan_ip} | Mode: {status.get('mode', '?')} | Recording ready: {status.get('ready', '?')}"
        except requests.exceptions.ConnectionError:
            return f"❌ Stack-chan offline (cannot reach {config.stackchan_ip})"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_playback_status() -> str:
        try:
            status = client.playback_status()
            return (
                "Playback "
                f"kind={status.get('kind', '?')} "
                f"playing={status.get('playing', '?')} "
                f"pcm_queue={status.get('queued_pcm_segments', '?')}/"
                f"{status.get('queued_pcm_bytes', '?')}B "
                f"audio_queue={status.get('audio_queue_depth', '?')} "
                f"mic={status.get('mic_state', '?')} "
                f"gesture={status.get('gesture', '?')} "
                f"heap={status.get('free_heap', '?')} "
                f"psram={status.get('free_psram', '?')}"
            )
        except requests.exceptions.ConnectionError:
            return f"❌ Stack-chan offline (cannot reach {config.stackchan_ip})"
        except Exception as exc:
            return f"❌ Error: {exc}"
