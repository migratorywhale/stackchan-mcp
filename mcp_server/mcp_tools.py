import json
import logging
import os
import shutil
import time

import requests

from . import audio_processing
from .audio_server import AUDIO_DIR, audio_url, start_audio_server
from .listening import capture_ready_recording, format_listen_result
from .stackchan_client import (
    PcmPlaybackError,
    StackchanClient,
    post_pcm_stream,
    post_pcm_tcp_stream,
    post_pcm_udp_stream,
)
from .stackchan_config import VALID_FACES, StackchanConfig, config_summary
from .telemetry import emit_event, new_request_id
from .voice_inbox import clear_events, format_events, read_events

logger = logging.getLogger(__name__)


def can_stream_pcm(config: StackchanConfig) -> bool:
    return (
        config.audio_mode != "wav"
        and config.tts_engine == "fish-audio"
        and bool(config.fish_audio_key)
    )


def timing_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def format_timing_ms(timings: dict[str, object]) -> str:
    parts = []
    for key, value in timings.items():
        if value is not None:
            parts.append(f"{key}={value}ms")
    return " timing[" + " ".join(parts) + "]" if parts else ""


def format_speech_confirmation(text: str) -> str:
    preview = text[:60]
    suffix = "…" if len(text) > 60 else ""
    return f"🗣️ Stack-chan is saying: \"{preview}{suffix}\""


def check_audio_dir() -> dict[str, object]:
    return {
        "path": str(AUDIO_DIR),
        "exists": AUDIO_DIR.exists(),
        "is_dir": AUDIO_DIR.is_dir(),
        "writable": os.access(AUDIO_DIR, os.W_OK),
    }


def build_health_report(client: StackchanClient, config: StackchanConfig) -> dict[str, object]:
    device: dict[str, object] = {
        "base_url": client.base_url,
        "ok": False,
    }
    try:
        device["audio_status"] = client.audio_status()
        device["ok"] = True
    except Exception as exc:
        device["audio_status_error"] = str(exc)
    try:
        device["playback_status"] = client.playback_status()
        device["ok"] = bool(device.get("ok"))
    except Exception as exc:
        device["playback_status_error"] = str(exc)

    report: dict[str, object] = {
        "ok": bool(device.get("ok")),
        "config": config_summary(config),
        "dependencies": {
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "edge_tts": bool(shutil.which(config.edge_tts_bin)),
            "fish_audio_key": bool(config.fish_audio_key),
        },
        "audio_dir": check_audio_dir(),
        "device": device,
    }
    return report


def post_preferred_pcm_stream(client: StackchanClient, text: str, lang: str, config: StackchanConfig) -> dict:
    def pcm_chunks():
        return audio_processing.iter_fish_pcm_stream(text, lang, config)

    if config.pcm_transport == "staged":
        result = post_pcm_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing)
        result.setdefault("transport", "staged")
        return result

    if config.pcm_transport in {"auto", "tcp"}:
        try:
            return post_pcm_tcp_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing)
        except PcmPlaybackError as exc:
            if exc.started or config.pcm_transport == "tcp":
                raise
            logger.warning("Falling back from TCP PCM to staged PCM: %s", exc)

    if config.pcm_transport == "udp":
        return post_pcm_udp_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing)

    result = post_pcm_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing)
    result.setdefault("transport", "staged")
    return result


def register_tools(mcp, client: StackchanClient, config: StackchanConfig, image_cls):
    @mcp.tool()
    def stackchan_say(text: str, lang: str = "zh") -> str:
        request_id = new_request_id()
        say_started = time.perf_counter()
        start_audio_server(config.audio_serve_port)

        try:
            pcm_fallback_reason = None
            if can_stream_pcm(config):
                pcm_started = time.perf_counter()
                try:
                    result = post_preferred_pcm_stream(client, text, lang, config)
                    if result.get("success"):
                        diag = (
                            f" transport={result.get('transport', 'staged')}"
                            f" session={result.get('session', '?')}"
                            f" segments={result.get('segments', '?')}"
                            f" bytes={result.get('total_bytes', '?')}"
                            f" gain={result.get('pcm_gain', '?')}"
                            f" limited={result.get('limited_samples', '?')}"
                            f" declicked={result.get('declicked_samples', '?')}"
                        )
                        timings = dict(result.get("timing_ms", {}))
                        timings["say_total"] = timing_ms(say_started)
                        emit_event(
                            "stackchan.say.completed",
                            body="PCM speech playback accepted",
                            request_id=request_id,
                            attributes={
                                "stackchan.audio.path": "pcm",
                                "stackchan.audio.mode": config.audio_mode,
                                "stackchan.pcm.transport": result.get("transport", "staged"),
                                "stackchan.tts.engine": config.tts_engine,
                                "stackchan.lang": lang,
                                "stackchan.text.length": len(text),
                                "stackchan.pcm.segments": result.get("segments"),
                                "stackchan.pcm.bytes": result.get("total_bytes"),
                                **{f"stackchan.latency.{key}_ms": value for key, value in timings.items()},
                            },
                        )
                        if result.get("saved_pcm"):
                            diag += f" saved={result['saved_pcm']}"
                        logger.info("PCM speech accepted: lang=%s%s%s", lang, diag, format_timing_ms(timings))
                        return format_speech_confirmation(text)
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
                logger.info("PCM attempt failed before playback after %sms; using WAV fallback", timing_ms(pcm_started))
                emit_event(
                    "stackchan.say.fallback.wav",
                    body="PCM failed before playback; using WAV fallback",
                    severity_text="WARN",
                    request_id=request_id,
                    attributes={
                        "stackchan.audio.mode": config.audio_mode,
                        "stackchan.tts.engine": config.tts_engine,
                        "stackchan.error": pcm_fallback_reason,
                        "stackchan.latency.pcm_attempt_ms": timing_ms(pcm_started),
                    },
                )
            elif config.audio_mode == "pcm":
                return "❌ PCM playback unavailable: TTS_ENGINE must be fish-audio and FISH_AUDIO_KEY must be set"

            wav_timing = {}
            t0 = time.perf_counter()
            wav_path = audio_processing.generate_tts(text, lang, config)
            wav_timing["tts"] = timing_ms(t0)
            t0 = time.perf_counter()
            audio_processing.validate_playback_wav(wav_path)
            wav_timing["validate"] = timing_ms(t0)
            baseline_started_ms = None
            baseline_playing = False
            try:
                t0 = time.perf_counter()
                baseline_status = client.playback_status()
                wav_timing["status"] = timing_ms(t0)
                baseline_started_ms = baseline_status.get("started_ms")
                baseline_playing = bool(baseline_status.get("playing"))
            except Exception as exc:
                logger.warning("Could not read playback status before /play: %s", exc)

            t0 = time.perf_counter()
            result = client.play(audio_url(config.mac_ip, config.audio_serve_port, wav_path.name))
            wav_timing["play_post"] = timing_ms(t0)

            if result.get("success"):
                if not baseline_playing:
                    t0 = time.perf_counter()
                    start_result = client.wait_for_playback_start(
                        baseline_started_ms=baseline_started_ms
                    )
                    wav_timing["playback_wait"] = timing_ms(t0)
                    if not start_result.get("started"):
                        status = start_result.get("status", {})
                        return (
                            "❌ Play was queued but playback did not start: "
                            f"kind={status.get('kind', '?')} "
                            f"playing={status.get('playing', '?')} "
                            f"current_bytes={status.get('current_bytes', '?')} "
                            f"started_ms={status.get('started_ms', '?')} "
                            f"deadline_ms={status.get('deadline_ms', '?')}"
                        )
                engine = "Fish Audio" if (config.tts_engine == "fish-audio" and config.fish_audio_key) else "edge-tts"
                fallback_note = " (PCM fallback)" if pcm_fallback_reason else ""
                wav_timing["say_total"] = timing_ms(say_started)
                emit_event(
                    "stackchan.say.completed",
                    body="WAV speech playback accepted",
                    request_id=request_id,
                    attributes={
                        "stackchan.audio.path": "wav",
                        "stackchan.audio.mode": config.audio_mode,
                        "stackchan.tts.engine": config.tts_engine,
                        "stackchan.lang": lang,
                        "stackchan.text.length": len(text),
                        "stackchan.fallback.used": bool(pcm_fallback_reason),
                        **{f"stackchan.latency.{key}_ms": value for key, value in wav_timing.items()},
                    },
                )
                logger.info(
                    "WAV speech accepted: engine=%s lang=%s mode=%s%s%s",
                    engine,
                    lang,
                    config.audio_mode,
                    fallback_note,
                    format_timing_ms(wav_timing),
                )
                return format_speech_confirmation(text)
            emit_event(
                "stackchan.say.failed",
                body="Playback request failed",
                severity_text="ERROR",
                request_id=request_id,
                attributes={
                    "stackchan.audio.path": "wav",
                    "stackchan.audio.mode": config.audio_mode,
                    "stackchan.error": str(result),
                    "stackchan.latency.say_total_ms": timing_ms(say_started),
                },
            )
            return f"❌ Play failed: {result}"
        except Exception as exc:
            emit_event(
                "stackchan.say.failed",
                body="Speech tool raised an exception",
                severity_text="ERROR",
                request_id=request_id,
                attributes={
                    "stackchan.audio.mode": config.audio_mode,
                    "stackchan.error": str(exc),
                    "stackchan.latency.say_total_ms": timing_ms(say_started),
                },
            )
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_listen(lang: str = "zh") -> str:
        try:
            result = capture_ready_recording(client, config, lang=lang, audio_dir=AUDIO_DIR)
            return format_listen_result(result)
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

    @mcp.tool(structured_output=False)
    def stackchan_see() -> list[object] | str:
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
    def stackchan_health() -> str:
        """Non-destructive health check for config, dependencies, and device status."""
        return json.dumps(build_health_report(client, config), ensure_ascii=False, indent=2)

    @mcp.tool()
    def stackchan_config_summary() -> str:
        """Return Stack-chan runtime config without secrets."""
        return json.dumps(config_summary(config), ensure_ascii=False, indent=2)

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
                f"download_queue={status.get('download_queue_depth', '?')} "
                f"download_in_flight={status.get('download_in_flight', '?')} "
                f"mic={status.get('mic_state', '?')} "
                f"gesture={status.get('gesture', '?')} "
                f"heap={status.get('free_heap', '?')} "
                f"psram={status.get('free_psram', '?')}"
            )
        except requests.exceptions.ConnectionError:
            return f"❌ Stack-chan offline (cannot reach {config.stackchan_ip})"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_voice_inbox(limit: int = 10) -> str:
        try:
            events = read_events(limit=limit)
            return format_events(events)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_voice_inbox_clear() -> str:
        try:
            clear_events()
            return "Stack-chan voice inbox cleared."
        except Exception as exc:
            return f"❌ Error: {exc}"
