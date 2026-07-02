#!/usr/bin/env python3
"""Experimental low-latency Stack-chan STT bridge.

This opens a host TCP listener, asks firmware `GET /stream?port=N` to push
16 kHz mono signed 16-bit PCM back to the host, then cuts one utterance with
RMS plus optional WebRTC VAD before handing a WAV file to Fish Audio ASR or
whisper-cli.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_server import audio_processing  # noqa: E402
from mcp_server.stackchan_client import StackchanClient  # noqa: E402
from mcp_server.stackchan_config import StackchanConfig, load_config  # noqa: E402
from mcp_server.voice_inbox import append_event, resolve_inbox_path  # noqa: E402
from scripts.stackchan_frontend_wake import DEFAULT_PROMPT_PREFIX  # noqa: E402
from scripts.stackchan_voice_bridge import (  # noqa: E402
    forward_event_to_frontend,
    load_env_file,
    load_frontend_token,
    should_append_to_inbox,
)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def print_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def rms_norm(frame: bytes) -> float:
    if not frame:
        return 0.0
    count = len(frame) // SAMPLE_WIDTH
    if count == 0:
        return 0.0
    total = 0.0
    for i in range(0, len(frame) - 1, SAMPLE_WIDTH):
        sample = int.from_bytes(frame[i : i + 2], "little", signed=True)
        x = sample / 32768.0
        total += x * x
    return math.sqrt(total / count)


def load_vad(mode: int):
    try:
        import webrtcvad  # type: ignore
    except ImportError:
        return None
    vad = webrtcvad.Vad()
    vad.set_mode(mode)
    return vad


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


def run_whisper(wav_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    whisper_bin = shutil.which(args.whisper_bin)
    if not whisper_bin:
        return {"skipped": True, "reason": f"{args.whisper_bin} not found"}

    cmd = [
        whisper_bin,
        "-f",
        str(wav_path),
        "-l",
        args.lang,
        "-nt",
    ]
    if args.whisper_extra:
        cmd.extend(args.whisper_extra)

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=args.whisper_timeout,
        check=False,
    )
    return {
        "skipped": False,
        "provider": "whisper-cli",
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def run_fish_asr(
    wav_path: Path,
    args: argparse.Namespace,
    config: StackchanConfig,
) -> dict[str, Any]:
    if not config.fish_audio_key:
        return {"skipped": True, "provider": "fish-audio", "reason": "FISH_AUDIO_KEY not set"}
    try:
        result = audio_processing.transcribe_audio(wav_path, args.lang, config)
    except Exception as exc:  # noqa: BLE001 - returned as diagnostic JSON
        return {"skipped": False, "provider": "fish-audio", "error": str(exc)}
    return {"skipped": False, "provider": "fish-audio", "result": result}


def run_asr(wav_path: Path, args: argparse.Namespace, config: StackchanConfig) -> dict[str, Any]:
    if args.no_transcribe or args.asr_provider == "none":
        return {"skipped": True, "provider": "none", "reason": "no_transcribe"}
    if args.asr_provider == "fish":
        return run_fish_asr(wav_path, args, config)
    if args.asr_provider == "whisper":
        return run_whisper(wav_path, args)

    fish = run_fish_asr(wav_path, args, config)
    if not fish.get("skipped") and not fish.get("error"):
        return fish
    whisper = run_whisper(wav_path, args)
    if not whisper.get("skipped"):
        return whisper
    return {
        "skipped": True,
        "provider": "auto",
        "fish": fish,
        "whisper": whisper,
    }


def extract_asr_text(asr: dict[str, Any]) -> tuple[str, str]:
    result = asr.get("result")
    if isinstance(result, dict):
        return str(result.get("text") or "").strip(), str(result.get("language") or "?")
    stdout = str(asr.get("stdout") or "").strip()
    return stdout, "?"


def dispatch_transcript_event(
    event: dict[str, Any],
    args: argparse.Namespace,
    inbox_path: Path | None,
) -> dict[str, Any]:
    if inbox_path is not None and should_append_to_inbox(event):
        append_event(event, inbox_path)
        event["inbox_appended"] = True
    frontend = forward_event_to_frontend(event, args)
    if frontend is not None:
        event["frontend"] = frontend
    return event


def cut_utterance_from_stream(conn: socket.socket, args: argparse.Namespace) -> dict[str, Any]:
    frame_bytes = int(SAMPLE_RATE * (args.frame_ms / 1000.0)) * SAMPLE_WIDTH
    silence_frames_needed = max(1, int(args.end_silence_ms / args.frame_ms))
    min_voice_frames = max(1, int(args.min_voice_ms / args.frame_ms))
    max_frames = max(1, int((args.seconds * 1000) / args.frame_ms))
    preroll_frames = max(0, int(args.preroll_ms / args.frame_ms))

    vad = load_vad(args.vad_mode) if args.use_webrtc_vad else None
    pre_roll: deque[bytes] = deque(maxlen=preroll_frames)
    captured = bytearray()
    pending = bytearray()
    started = False
    voice_frames = 0
    silence_frames = 0
    total_frames = 0
    rms_sum = 0.0
    rms_max = 0.0
    deadline = time.monotonic() + args.seconds + 2

    while total_frames < max_frames and time.monotonic() < deadline:
        while len(pending) < frame_bytes:
            chunk = conn.recv(4096)
            if not chunk:
                break
            pending.extend(chunk)
        if len(pending) < frame_bytes:
            break

        frame = bytes(pending[:frame_bytes])
        del pending[:frame_bytes]
        total_frames += 1
        rms = rms_norm(frame)
        rms_sum += rms
        rms_max = max(rms_max, rms)
        vad_speech = vad.is_speech(frame, SAMPLE_RATE) if vad else True
        is_voice = rms >= args.start_rms and vad_speech

        if not started:
            pre_roll.append(frame)
            if is_voice:
                voice_frames += 1
                if voice_frames >= min_voice_frames:
                    started = True
                    for old_frame in pre_roll:
                        captured.extend(old_frame)
                    pre_roll.clear()
                    silence_frames = 0
            else:
                voice_frames = 0
            continue

        captured.extend(frame)
        if rms < args.end_rms or not vad_speech:
            silence_frames += 1
        else:
            silence_frames = 0
        if silence_frames >= silence_frames_needed:
            break

    with suppress(Exception):
        conn.shutdown(socket.SHUT_RDWR)
    conn.close()
    return {
        "pcm": bytes(captured),
        "started": started,
        "frames": total_frames,
        "duration": len(captured) / (SAMPLE_RATE * SAMPLE_WIDTH),
        "vad": "webrtc+rms" if vad else "rms",
        "rms_avg": rms_sum / total_frames if total_frames else 0.0,
        "rms_max": rms_max,
    }


def run_once(args: argparse.Namespace) -> int:
    started_at = time.monotonic()
    timing_ms: dict[str, int] = {}

    def mark(name: str) -> None:
        timing_ms[name] = round((time.monotonic() - started_at) * 1000)

    config = load_config()
    client = StackchanClient(config)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inbox_path = None if args.no_inbox else resolve_inbox_path(args.inbox)
    mark("configured")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.listen_host, args.port))
        listener.listen(1)
        listen_port = listener.getsockname()[1]
        listener.settimeout(args.accept_timeout)
        mark("listener_ready")

        stream_result: dict[str, Any] = {}
        stream_error: str | None = None

        def trigger_stream() -> None:
            nonlocal stream_result, stream_error
            try:
                mark("stream_request_started")
                stream_result = client.stream_to_host(
                    host=args.device_connect_host,
                    port=listen_port,
                    seconds=args.seconds,
                    frame_samples=int(SAMPLE_RATE * args.frame_ms / 1000),
                )
                mark("stream_request_finished")
            except Exception as exc:  # noqa: BLE001 - printed as diagnostic JSON
                stream_error = str(exc)
                mark("stream_request_failed")

        thread = threading.Thread(target=trigger_stream, daemon=True)
        thread.start()

        try:
            conn, addr = listener.accept()
            mark("tcp_connected")
        except TimeoutError:
            thread.join(timeout=1)
            print_event(
                {
                    "type": "error",
                    "timestamp": utc_now(),
                    "error": "timed out waiting for firmware TCP stream",
                    "timing_ms": timing_ms,
                    "stream_result": stream_result,
                    "stream_error": stream_error,
                }
            )
            return 1

        conn.settimeout(args.socket_timeout)
        capture = cut_utterance_from_stream(conn, args)
        mark("capture_done")
        thread.join(timeout=2)
        mark("stream_thread_joined")

    if not capture["started"] or not capture["pcm"]:
        print_event(
            {
                "type": "idle",
                "timestamp": utc_now(),
                "peer": addr[0],
                "frames": capture["frames"],
                "vad": capture["vad"],
                "rms_avg": capture["rms_avg"],
                "rms_max": capture["rms_max"],
                "timing_ms": timing_ms,
                "stream_result": stream_result,
                "stream_error": stream_error,
            }
        )
        return 0

    wav_path = out_dir / f"stream_{int(time.time() * 1000)}.wav"
    write_wav(wav_path, capture["pcm"])
    mark("wav_written")
    asr = run_asr(wav_path, args, config)
    mark("asr_done")
    text, detected_language = extract_asr_text(asr)
    event_type = "transcript" if text else "idle"
    event = {
        "type": event_type,
        "timestamp": utc_now(),
        "source": "stackchan_stream",
        "text": text,
        "detected_language": detected_language,
        "empty_transcript": not bool(text),
        "peer": addr[0],
        "wav_path": str(wav_path),
        "audio_bytes": len(capture["pcm"]),
        "duration": capture["duration"],
        "vad": capture["vad"],
        "rms_avg": capture["rms_avg"],
        "rms_max": capture["rms_max"],
        "timing_ms": timing_ms,
        "stream_result": stream_result,
        "stream_error": stream_error,
        "asr": asr,
    }
    dispatched = dispatch_transcript_event(event, args, inbox_path)
    mark("dispatch_done")
    dispatched["timing_ms"] = timing_ms
    print_event(dispatched)
    return 0 if not stream_error else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Experimental Stack-chan PCM stream STT bridge.")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument(
        "--device-connect-host", default="", help="Host/IP the firmware should connect to."
    )
    parser.add_argument("--port", type=int, default=0, help="Host TCP port. 0 chooses a free port.")
    parser.add_argument("--seconds", type=int, default=10, help="Maximum firmware stream duration.")
    parser.add_argument("--accept-timeout", type=float, default=5)
    parser.add_argument("--socket-timeout", type=float, default=3)
    parser.add_argument("--frame-ms", type=int, default=20, choices=[10, 20, 30])
    parser.add_argument("--start-rms", type=float, default=0.003)
    parser.add_argument("--end-rms", type=float, default=0.0015)
    parser.add_argument("--min-voice-ms", type=int, default=120)
    parser.add_argument("--end-silence-ms", type=int, default=900)
    parser.add_argument("--preroll-ms", type=int, default=300)
    parser.add_argument("--use-webrtc-vad", action="store_true")
    parser.add_argument("--vad-mode", type=int, default=2, choices=[0, 1, 2, 3])
    parser.add_argument("--out-dir", default="/tmp/stackchan_audio/stream")
    parser.add_argument(
        "--inbox",
        help="JSONL inbox path for transcript events. Default: /tmp/stackchan_audio/voice_inbox.jsonl",
    )
    parser.add_argument(
        "--no-inbox", action="store_true", help="Do not append transcripts to inbox"
    )
    parser.add_argument("--no-transcribe", action="store_true")
    parser.add_argument(
        "--asr-provider",
        choices=["auto", "fish", "whisper", "none"],
        default="auto",
        help="Transcription backend. auto uses Fish Audio when configured, then whisper-cli.",
    )
    parser.add_argument("--whisper-bin", default="whisper-cli")
    parser.add_argument("--whisper-timeout", type=float, default=60)
    parser.add_argument("--lang", default="zh")
    parser.add_argument("--whisper-extra", nargs=argparse.REMAINDER, default=[])
    parser.add_argument(
        "--wake-url",
        default=os.environ.get("STACKCHAN_FRONTEND_WAKE_URL", ""),
        help="agent-host /wake URL. If omitted with a wake session, defaults to http://127.0.0.1:3200/wake.",
    )
    parser.add_argument(
        "--wake-session-id",
        default=os.environ.get("STACKCHAN_FRONTEND_SESSION_ID", ""),
        help="Frontend session UUID, or latest/auto when STACKCHAN_FRONTEND_REGISTRY is configured.",
    )
    parser.add_argument(
        "--wake-session-title",
        default=os.environ.get("STACKCHAN_FRONTEND_SESSION_TITLE", ""),
        help="Resolve the latest non-archived frontend session whose title contains this text.",
    )
    parser.add_argument("--wake-token", default=os.environ.get("STACKCHAN_FRONTEND_TOKEN", ""))
    parser.add_argument("--wake-model", default=os.environ.get("STACKCHAN_FRONTEND_MODEL", ""))
    parser.add_argument(
        "--wake-timeout",
        type=float,
        default=float(os.environ.get("STACKCHAN_FRONTEND_TIMEOUT", "10")),
    )
    parser.add_argument(
        "--wake-retries",
        type=int,
        default=int(os.environ.get("STACKCHAN_FRONTEND_RETRIES", "0")),
        help="Retry /wake when agent-host returns 409 busy. Default: 0.",
    )
    parser.add_argument(
        "--wake-retry-delay",
        type=float,
        default=float(os.environ.get("STACKCHAN_FRONTEND_RETRY_DELAY", "3")),
    )
    parser.add_argument(
        "--wake-quiet-minutes",
        type=int,
        default=int(os.environ.get("STACKCHAN_FRONTEND_QUIET_MINUTES", "0")),
    )
    parser.add_argument(
        "--wake-no-force",
        action="store_true",
        help="Respect agent-host quiet_minutes instead of forcing the voice prompt through.",
    )
    parser.add_argument(
        "--prompt-prefix",
        default=os.environ.get("STACKCHAN_FRONTEND_PROMPT_PREFIX", DEFAULT_PROMPT_PREFIX),
    )
    parser.add_argument(
        "--wake-words",
        default=os.environ.get("STACKCHAN_VOICE_WAKE_WORDS", ""),
        help=(
            "Comma-separated activation words. If set, frontend forwarding only happens when "
            "the transcript starts with one of these words; inbox logging still happens."
        ),
    )
    return parser


def main() -> int:
    load_env_file(REPO_ROOT / ".env")
    load_frontend_token()
    return run_once(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
