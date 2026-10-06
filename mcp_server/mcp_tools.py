import json
import logging
import os
import shutil
import time
from typing import Any

import requests
from pydantic import StrictBool

from . import audio_processing
from .audio_publish import publish_wav
from .audio_server import AUDIO_DIR, audio_url, start_audio_server
from .face_tracking import signal_face_tracking
from .listening import capture_ready_recording, format_listen_result
from .stackchan_client import (
    PcmPlaybackError,
    StackchanClient,
    post_pcm_stream,
    post_pcm_tcp_stream,
    post_pcm_udp_stream,
)
from .stackchan_config import VALID_FACES, StackchanConfig, config_summary, env_float
from .telemetry import emit_event, new_request_id
from .voice_inbox import clear_events, format_events, read_events

logger = logging.getLogger(__name__)


def _booth_mode_report(enabled: bool | None, status: Any, error: str | None = None) -> str:
    fields = ("success", "booth_mode", "persisted", "capture_allowed", "mic_running")
    problems = []
    valid = isinstance(status, dict) and all(type(status.get(key)) is bool for key in fields)
    if error:
        problems.append(error)
    if not valid or status.get("success") is not True:
        problems.append("Device did not return a complete successful boolean booth status")
    else:
        if enabled is not None and status["booth_mode"] is not enabled:
            problems.append("Reported booth mode does not match the requested state")
        if not status["persisted"]:
            problems.append("Persistence across reboot is not confirmed")
        if status["booth_mode"] and (status["capture_allowed"] or status["mic_running"]):
            problems.append("Device microphone capture is not confirmed blocked")
        if not status["booth_mode"] and not status["capture_allowed"]:
            problems.append("Normal microphone capture is not confirmed allowed")
    report = {
        "success": not problems,
        "operation": "query" if enabled is None else "set",
        "requested": enabled,
        "outcome": "confirmed" if not problems else ("partial" if valid else "unknown"),
        "status": status,
    }
    if problems:
        report["error"] = "; ".join(problems)
        report["note"] = (
            "State may have changed or only partially applied. Query to verify; no write was retried."
            if enabled is not None else "Current durable booth state is unconfirmed."
        )
    return json.dumps(report, ensure_ascii=False)


def can_stream_pcm(config: StackchanConfig) -> bool:
    return (
        config.audio_mode != "wav"
        and (
            (config.tts_engine == "fish-audio" and bool(config.fish_audio_key))
            or (config.tts_engine == "elevenlabs" and bool(
                (config.elevenlabs_api_key and config.elevenlabs_voice_id) or config.fish_audio_key
            ))
        )
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


def speech_tracking_duration(text: str) -> float:
    # Start looking while TTS is generated, then stay engaged for the estimated
    # spoken duration. Slow remote camera links can set a larger deployment
    # minimum through the same lease-duration setting used by voice triggers.
    configured_minimum = max(
        1.0,
        min(env_float("STACKCHAN_FACE_TRACK_DURATION_SEC", 8.0), 30.0),
    )
    return max(configured_minimum, min(30.0, 3.0 + len(text) / 4.0))


def audit_tool_call(name: str, **attributes: object) -> None:
    parts = []
    for key, value in attributes.items():
        if value is not None:
            parts.append(f"{key}={value!r}")
    suffix = " " + " ".join(parts) if parts else ""
    logger.info("Stack-chan MCP tool call: tool=%s%s", name, suffix)


def check_audio_dir() -> dict[str, object]:
    return {
        "path": str(AUDIO_DIR),
        "exists": AUDIO_DIR.exists(),
        "is_dir": AUDIO_DIR.is_dir(),
        "writable": os.access(AUDIO_DIR, os.W_OK),
    }


def format_device_request_error(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return "Stack-chan request timed out; it may be busy or the link may be slow. Availability unconfirmed."
    if isinstance(exc, requests.ConnectionError):
        return "Stack-chan is unreachable from MCP right now (connection failed). Availability unconfirmed."
    return f"Device request failed: {exc}"


def build_health_report(client: Any, config: StackchanConfig) -> dict[str, object]:
    device: dict[str, object] = {
        "base_url": client.base_url,
        "ok": False,
    }
    if config.transport == "relay":
        try:
            relay = client.relay_status(timeout=config.http_probe_timeout)
            device["relay"] = relay
            device["ok"] = relay.get("online") is True
        except requests.RequestException as exc:
            device["relay_error"] = format_device_request_error(exc)
        except Exception as exc:
            device["relay_error"] = str(exc)
        device["reachability"] = "responding" if device["ok"] else "unconfirmed"
        device["all_checks_ok"] = device["ok"]
    else:
        _probe_direct_device(client, config, device)

    return {
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


def _probe_direct_device(client: Any, config: StackchanConfig, device: dict[str, object]) -> None:
    try:
        device["audio_status"] = client.audio_status(timeout=config.http_probe_timeout)
        device["ok"] = True
    except requests.RequestException as exc:
        device["audio_status_error"] = format_device_request_error(exc)
    except Exception as exc:
        device["audio_status_error"] = str(exc)
    try:
        device["playback_status"] = client.playback_status(timeout=config.http_probe_timeout)
        device["ok"] = True
    except requests.RequestException as exc:
        device["playback_status_error"] = format_device_request_error(exc)
    except Exception as exc:
        device["playback_status_error"] = str(exc)
    device["reachability"] = "responding" if device["ok"] else "unconfirmed"
    device["all_checks_ok"] = "audio_status" in device and "playback_status" in device


def post_preferred_pcm_stream(
    client: StackchanClient, text: str, lang: str, config: StackchanConfig
) -> dict:
    prepared_pcm = None
    engine = config.tts_engine
    synthesis_started = time.perf_counter()
    if engine == "elevenlabs":
        prepared_pcm, engine = audio_processing.prepare_elevenlabs_pcm(text, lang, config)
    synthesis_ms = timing_ms(synthesis_started)

    def pcm_chunks():
        if prepared_pcm is not None:
            return (prepared_pcm[offset:offset + 4096] for offset in range(0, len(prepared_pcm), 4096))
        return audio_processing.iter_fish_pcm_stream(text, lang, config)

    def report(result: dict) -> dict:
        result["tts_engine"] = engine
        if prepared_pcm is not None:
            result.setdefault("timing_ms", {})["tts_prepare"] = synthesis_ms
        return result

    if config.transport == "relay" or config.pcm_transport == "staged":
        result = post_pcm_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing)
        result.setdefault("transport", "relay" if config.transport == "relay" else "staged")
        return report(result)

    if config.pcm_transport in {"auto", "tcp"}:
        try:
            return report(post_pcm_tcp_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing))
        except PcmPlaybackError as exc:
            if exc.started or config.pcm_transport == "tcp":
                raise
            logger.warning("Falling back from TCP PCM to staged PCM: %s", exc)

    if config.pcm_transport == "udp":
        return report(post_pcm_udp_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing))

    result = post_pcm_stream(client, pcm_chunks(), AUDIO_DIR, audio_processing)
    result.setdefault("transport", "staged")
    return report(result)


def register_tools(mcp, client: Any, config: StackchanConfig, image_cls):
    @mcp.tool()
    def stackchan_say(text: str, lang: str = "zh") -> str:
        request_id = new_request_id()
        say_started = time.perf_counter()
        audit_tool_call(
            "stackchan_say",
            lang=lang,
            text_len=len(text),
        )
        if config.transport == "direct":
            signal_face_tracking(
                "stackchan_say",
                duration=speech_tracking_duration(text),
            )
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
                            f" tts={result.get('tts_engine', config.tts_engine)}"
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
                                "stackchan.tts.engine": result.get("tts_engine", config.tts_engine),
                                "stackchan.tts.requested_engine": config.tts_engine,
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
                        confirmation = format_speech_confirmation(text)
                        if config.tts_engine == "elevenlabs" and result.get("tts_engine") == "fish-audio":
                            confirmation += " (Fish fallback)"
                        return confirmation
                    pcm_fallback_reason = f"PCM play returned {result}"
                    if config.audio_mode == "pcm" or config.transport == "relay" or config.tts_engine == "elevenlabs":
                        return f"❌ PCM play failed: {result}"
                    logger.warning("Falling back to WAV TTS: %s", pcm_fallback_reason)
                except PcmPlaybackError as exc:
                    if exc.started:
                        logger.error("PCM playback failed after audio started: %s", exc)
                        return f"❌ PCM playback failed after audio started: {exc}"
                    pcm_fallback_reason = str(exc)
                    if config.audio_mode == "pcm" or config.transport == "relay" or config.tts_engine == "elevenlabs":
                        logger.error("PCM playback failed in forced PCM mode: %s", exc)
                        return f"❌ PCM playback failed: {exc}"
                    logger.warning("Falling back to WAV TTS after PCM failure: %s", exc)
                except Exception as exc:
                    pcm_fallback_reason = str(exc)
                    if config.audio_mode == "pcm" or config.transport == "relay" or config.tts_engine == "elevenlabs":
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
            elif config.audio_mode == "pcm" and config.transport != "relay":
                return "❌ PCM playback unavailable: configure fish-audio or elevenlabs TTS credentials"

            wav_timing = {}
            tts_details: dict[str, str] = {}
            t0 = time.perf_counter()
            if config.tts_engine == "elevenlabs":
                wav_path = audio_processing.generate_tts(text, lang, config, details=tts_details)
            else:
                wav_path = audio_processing.generate_tts(text, lang, config)
            engine = tts_details.get("engine", config.tts_engine)
            provider_fallback = config.tts_engine == "elevenlabs" and engine == "fish-audio"
            confirmation = format_speech_confirmation(text) + (" (Fish fallback)" if provider_fallback else "")
            wav_timing["tts"] = timing_ms(t0)
            t0 = time.perf_counter()
            audio_processing.validate_playback_wav(wav_path)
            wav_timing["validate"] = timing_ms(t0)
            if config.transport == "relay":
                result = post_pcm_stream(
                    client, audio_processing.iter_wav_pcm(wav_path), AUDIO_DIR, audio_processing
                )
                if result.get("success"):
                    logger.info("Relay WAV speech accepted: tts=%s lang=%s", engine, lang)
                    return confirmation
                return f"PCM play failed: {result}"
            if config.audio_publish_target:
                t0 = time.perf_counter()
                publish_wav(
                    wav_path,
                    config.audio_publish_target,
                    config.audio_publish_timeout,
                    config.audio_publish_attempts,
                )
                wav_timing["publish"] = timing_ms(t0)
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
                        # 2026/9/2假红教训：队列ack成功后轮询未确认≠没播。
                        # 高延迟链路(exit node/蜂窝)下短句常在轮询间隙播完。降级为⚠️，不再报❌。
                        return (
                            "⚠️ Play queued OK; start not confirmed within poll window "
                            "(likely latency — clip may have already played): "
                            f"kind={status.get('kind', '?')} "
                            f"playing={status.get('playing', '?')} "
                            f"current_bytes={status.get('current_bytes', '?')} "
                            f"started_ms={status.get('started_ms', '?')} "
                            f"deadline_ms={status.get('deadline_ms', '?')}"
                        )
                fallback_note = " (PCM fallback)" if pcm_fallback_reason else ""
                wav_timing["say_total"] = timing_ms(say_started)
                emit_event(
                    "stackchan.say.completed",
                    body="WAV speech playback accepted",
                    request_id=request_id,
                    attributes={
                        "stackchan.audio.path": "wav",
                        "stackchan.audio.mode": config.audio_mode,
                        "stackchan.tts.engine": engine,
                        "stackchan.tts.requested_engine": config.tts_engine,
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
                return confirmation
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
        audit_tool_call("stackchan_listen", lang=lang)
        try:
            result = capture_ready_recording(client, config, lang=lang, audio_dir=AUDIO_DIR)
            return format_listen_result(result)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_move(x: float = 0, y: float = 0, speed: int = 50) -> str:
        audit_tool_call("stackchan_move", x=x, y=y, speed=speed)
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
        audit_tool_call("stackchan_nod")
        try:
            result = client.gesture("nod")
            return "🤖 *nods yes*" if result.get("success") else f"❌ Nod failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_shake() -> str:
        audit_tool_call("stackchan_shake")
        try:
            result = client.gesture("shake")
            return "🤖 *shakes head no*" if result.get("success") else f"❌ Shake failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_face(expression: str = "calm") -> str:
        audit_tool_call("stackchan_face", expression=expression)
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
        audit_tool_call("stackchan_see")
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
        except requests.RequestException as exc:
            return format_device_request_error(exc)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_home() -> str:
        audit_tool_call("stackchan_home")
        try:
            result = client.gesture("home")
            return "🤖 Head returned to home position" if result.get("success") else f"❌ Home failed: {result}"
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_status() -> str:
        audit_tool_call("stackchan_status")
        try:
            if config.transport == "relay":
                status = client.relay_status(timeout=config.http_probe_timeout)
                return (
                    f"Stack-chan relay online={status.get('online', False)} "
                    f"busy={status.get('busy', False)} last_seen_age={status.get('last_seen_age')}"
                )
            status = client.audio_status(timeout=config.http_probe_timeout)
            return f"✅ Stack-chan online at {config.stackchan_ip} | Mode: {status.get('mode', '?')} | Recording ready: {status.get('ready', '?')}"
        except requests.RequestException as exc:
            return format_device_request_error(exc)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_booth_mode(enabled: StrictBool | None = None) -> str:
        """Query booth mode when omitted/null; explicitly set it with a JSON boolean.

        Never toggles implicitly or retries writes. Booth mode persists across reboot,
        blocks ALL device microphone capture, and clears pending local recording.
        Touch gives visual feedback only. The speaker still works and the camera is
        unchanged; the phone supplies input. Disabling resumes normal device capture.
        Already-downloaded audio cannot be recalled. Check success and all reported
        status fields: a failed/partial/unknown result does not confirm privacy or
        persistence. After an uncertain write, query before deciding what to do next.
        """
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled must be a JSON boolean or null (query)")
        audit_tool_call("stackchan_booth_mode", enabled=enabled)
        try:
            status = client.get_booth_mode() if enabled is None else client.set_booth_mode(enabled)
            return _booth_mode_report(enabled, status)
        except requests.HTTPError as exc:
            status = None
            detail = str(exc)
            if exc.response is not None:
                detail = f"HTTP {exc.response.status_code}: {detail}"
                try:
                    status = exc.response.json()
                except ValueError:
                    detail += f"; body={exc.response.text[:500]}"
            return _booth_mode_report(enabled, status, detail)
        except Exception as exc:
            return _booth_mode_report(enabled, None, f"Request failed: {exc}")

    @mcp.tool()
    def stackchan_health() -> str:
        """Non-destructive health check for config, dependencies, and device status."""
        audit_tool_call("stackchan_health")
        return json.dumps(build_health_report(client, config), ensure_ascii=False, indent=2)

    @mcp.tool()
    def stackchan_config_summary() -> str:
        """Return Stack-chan runtime config without secrets."""
        audit_tool_call("stackchan_config_summary")
        return json.dumps(config_summary(config), ensure_ascii=False, indent=2)

    @mcp.tool()
    def stackchan_playback_status() -> str:
        audit_tool_call("stackchan_playback_status")
        try:
            status = client.playback_status(timeout=config.http_probe_timeout)
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
        except requests.RequestException as exc:
            return format_device_request_error(exc)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_sense() -> str:
        """Read environmental sensor data from the robot: temperature, humidity, and barometric
        pressure. Use this to understand the physical conditions around Stack-chan. A falling
        barometric pressure reading often precedes weather changes and headaches — worth noting
        when someone reports feeling unwell. Values are returned only for sensors that report
        a valid reading; missing or NaN fields are silently omitted."""
        audit_tool_call("stackchan_sense")
        try:
            data = client.read_env()
            parts = []
            temp = data.get("temperature")
            if temp is not None:
                try:
                    if temp == temp:  # NaN check
                        parts.append(f"🌡️ {float(temp):.1f}°C")
                except (TypeError, ValueError):
                    pass
            humidity = data.get("humidity")
            if humidity is not None:
                try:
                    if humidity == humidity:
                        parts.append(f"💧 {float(humidity):.1f}%")
                except (TypeError, ValueError):
                    pass
            pressure = data.get("pressure")
            if pressure is not None:
                try:
                    if pressure == pressure:
                        parts.append(f"🔽 {float(pressure):.1f} hPa")
                except (TypeError, ValueError):
                    pass
            if not parts:
                return "⚠️ No sensor data available"
            return "  ".join(parts)
        except requests.RequestException as exc:
            return format_device_request_error(exc)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_voice_inbox(limit: int = 10) -> str:
        audit_tool_call("stackchan_voice_inbox", limit=limit)
        try:
            events = read_events(limit=limit)
            return format_events(events)
        except Exception as exc:
            return f"❌ Error: {exc}"

    @mcp.tool()
    def stackchan_voice_inbox_clear() -> str:
        audit_tool_call("stackchan_voice_inbox_clear")
        try:
            clear_events()
            return "Stack-chan voice inbox cleared."
        except Exception as exc:
            return f"❌ Error: {exc}"
