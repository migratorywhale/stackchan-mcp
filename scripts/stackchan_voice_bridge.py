#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.stackchan_frontend_session import (  # noqa: E402
    SessionResolutionError,
    load_sessions,
    select_session,
)
from scripts.stackchan_frontend_wake import (  # noqa: E402
    DEFAULT_PROMPT_PREFIX,
    forward_to_frontend,
    parse_wake_words,
)

DEFAULT_TOUCH_PROMPT_PREFIX = "[Stack-chan语音输入] （触摸）"
TOUCH_PET_TEXT = "[Stack-chan触摸]"
MAX_TOUCH_PET_EVENTS_PER_POLL = 4
MAX_TOUCH_PET_BACKLOG = 32


class TouchPetTracker:
    """Turn the firmware's monotonic pet counter into one-shot host events."""

    def __init__(
        self,
        max_events_per_poll: int = MAX_TOUCH_PET_EVENTS_PER_POLL,
        max_backlog: int = MAX_TOUCH_PET_BACKLOG,
    ):
        self._last_count: int | None = None
        self._max_events_per_poll = max(1, max_events_per_poll)
        self._max_backlog = max(self._max_events_per_poll, max_backlog)
        self._pending_events = 0

    def observe(self, status: dict[str, Any]) -> int:
        count = status.get("touch_pet_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return 0
        if self._last_count is None or count < self._last_count:
            self._last_count = count
            self._pending_events = 0
            return 0

        new_events = count - self._last_count
        self._last_count = count
        self._pending_events = min(self._pending_events + new_events, self._max_backlog)
        emitted = min(self._pending_events, self._max_events_per_poll)
        self._pending_events -= emitted
        return emitted


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def read_frontend_token_file() -> str:
    env_path_raw = os.environ.get("STACKCHAN_FRONTEND_ENV", "")
    if not env_path_raw:
        return ""
    env_path = Path(env_path_raw)
    if not env_path.exists():
        return ""

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "AGENT_HOST_TOKEN":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            return value
        return ""
    return ""


def load_frontend_token() -> None:
    if os.environ.get("STACKCHAN_FRONTEND_TOKEN"):
        return
    value = read_frontend_token_file()
    if value:
        os.environ["STACKCHAN_FRONTEND_TOKEN"] = value


def resolve_frontend_token(configured_token: str = "") -> str:
    if configured_token:
        return configured_token
    return read_frontend_token_file() or os.environ.get("STACKCHAN_FRONTEND_TOKEN", "")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def print_event(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def default_consumer_lock_path(stackchan_ip: str, stackchan_port: int) -> Path:
    target = f"{stackchan_ip}:{stackchan_port}"
    target_id = hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]
    return Path.home() / "Library" / "Caches" / "stackchan" / f"voice-bridge-{target_id}.lock"


def acquire_consumer_lock(
    lock_path: Path,
    *,
    wait_interval: float = 5.0,
    wait: bool = True,
) -> TextIO | None:
    """Serialize consumers of Stack-chan's destructive GET /audio endpoint."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    waiting_reported = False

    while True:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if not wait:
                print_event(
                    {
                        "type": "busy",
                        "timestamp": utc_now(),
                        "reason": "another voice bridge owns the audio consumer lock",
                        "lock_file": str(lock_path),
                    }
                )
                lock_handle.close()
                return None
            if not waiting_reported:
                print_event(
                    {
                        "type": "standby",
                        "timestamp": utc_now(),
                        "reason": "another voice bridge owns the audio consumer lock",
                        "lock_file": str(lock_path),
                    }
                )
                waiting_reported = True
            time.sleep(max(0.1, wait_interval))

    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(f"{os.getpid()}\n")
    lock_handle.flush()
    return lock_handle


def should_append_to_inbox(event: dict) -> bool:
    return event.get("type") == "transcript" and bool(str(event.get("text") or "").strip())


def recording_source_from_result(result: dict[str, Any]) -> str:
    status = result.get("status")
    if isinstance(status, dict) and str(status.get("source") or "").lower() == "touch":
        return "touch"
    return "voice"


def resolve_wake_session(session_id: str, title: str = "") -> str:
    if title:
        session = select_session(load_sessions(), title=title)
        if not session:
            raise SessionResolutionError(f"no matching frontend session title: {title}")
        return str(session.get("id") or "")
    if session_id in {"latest", "auto"}:
        session = select_session(load_sessions())
        if not session:
            raise SessionResolutionError("no non-archived frontend session found")
        return str(session.get("id") or "")
    return session_id


def forward_event_to_frontend(event: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        wake_session_id = resolve_wake_session(args.wake_session_id, args.wake_session_title)
    except SessionResolutionError as exc:
        return {"ok": False, "skipped": f"frontend session not resolved: {exc}"}
    wake_url = args.wake_url
    if not wake_url and wake_session_id:
        wake_url = "http://127.0.0.1:3200/wake"
    if not wake_url and not wake_session_id:
        return None
    source = str(event.get("source") or "")
    is_touch_recording = (
        str(event.get("recording_source") or "").lower() == "touch"
        or source == "stackchan_touch"
    )
    is_touch_pet = str(event.get("interaction") or "") == "petting"
    result = forward_to_frontend(
        event,
        wake_url=wake_url,
        session_id=wake_session_id,
        token=resolve_frontend_token(args.wake_token),
        model=args.wake_model,
        timeout=args.wake_timeout,
        retries=args.wake_retries,
        retry_delay=args.wake_retry_delay,
        force=not args.wake_no_force,
        quiet_minutes=args.wake_quiet_minutes,
        prompt_prefix=(
            ""
            if is_touch_pet
            else (
                getattr(args, "touch_prompt_prefix", DEFAULT_TOUCH_PROMPT_PREFIX)
                if is_touch_recording
                else args.prompt_prefix
            )
        ),
        wake_words=() if is_touch_recording or is_touch_pet else parse_wake_words(args.wake_words),
        source=source or "stackchan_mic",
    )
    if result.get("ok") and not is_touch_pet:
        from mcp_server.face_tracking import signal_face_tracking

        signal_face_tracking("touch_voice" if is_touch_recording else "wake_word")
    return result


def build_touch_pet_event() -> dict[str, Any]:
    return {
        "type": "touch",
        "source": "stackchan_touch_strip",
        "interaction": "petting",
        "timestamp": utc_now(),
        "text": TOUCH_PET_TEXT,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Poll Stack-chan's microphone endpoint and print transcribed recordings as JSONL. "
            "This is a host-side bridge prototype; it does not dispatch to Claude Code yet."
        )
    )
    parser.add_argument("--lang", default="zh", help="ASR language passed to Fish Audio, default: zh")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds")
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Stop after this many consumed recordings. 0 means run until interrupted.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once, then exit. If audio is ready, this consumes the recording.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only check /audio/status. Does not consume GET /audio or run ASR.",
    )
    parser.add_argument(
        "--verbose-idle",
        action="store_true",
        help="Print idle status events when no recording is ready.",
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("STACKCHAN_VOICE_BRIDGE_LOCKFILE", ""),
        help=(
            "Host-local lock that ensures only one bridge consumes GET /audio. "
            "Defaults to a target-specific file under Library/Caches/stackchan. "
            "Dry-run probes do not take the lock."
        ),
    )
    parser.add_argument(
        "--lock-wait-interval",
        type=float,
        default=float(os.environ.get("STACKCHAN_VOICE_BRIDGE_LOCK_WAIT", "5")),
        help="Seconds a standby bridge waits before retrying the consumer lock.",
    )
    parser.add_argument(
        "--inbox",
        help="JSONL inbox path for transcript events. Default: /tmp/stackchan_audio/voice_inbox.jsonl",
    )
    parser.add_argument(
        "--no-inbox",
        action="store_true",
        help="Do not append transcript events to the local voice inbox.",
    )
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
        "--touch-prompt-prefix",
        default=os.environ.get("STACKCHAN_TOUCH_PROMPT_PREFIX", DEFAULT_TOUCH_PROMPT_PREFIX),
        help="Prefix for touch-triggered transcripts. These recordings bypass wake-word matching.",
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
    from mcp_server.listening import capture_ready_recording
    from mcp_server.stackchan_client import StackchanClient
    from mcp_server.stackchan_config import load_config
    from mcp_server.voice_inbox import append_event, resolve_inbox_path

    load_env_file(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    config = load_config()
    client = StackchanClient(config)
    consumed = 0
    touch_pet_tracker = TouchPetTracker()
    inbox_path = None if args.no_inbox else resolve_inbox_path(args.inbox)
    # Keep the descriptor reachable for the whole polling loop. Process exit
    # releases the advisory lock even when launchd terminates the bridge.
    _consumer_lock = None
    if not args.dry_run:
        lock_path = (
            Path(args.lock_file).expanduser()
            if args.lock_file
            else default_consumer_lock_path(config.stackchan_ip, config.stackchan_port)
        )
        try:
            _consumer_lock = acquire_consumer_lock(
                lock_path,
                wait_interval=args.lock_wait_interval,
                wait=not args.once,
            )
        except KeyboardInterrupt:
            print_event({"type": "stop", "timestamp": utc_now(), "reason": "keyboard_interrupt"})
            return 0
        if _consumer_lock is None:
            return 2

    while True:
        try:
            touch_pet_events = 0
            if args.dry_run:
                status = client.audio_status()
                event = {
                    "type": "status",
                    "timestamp": utc_now(),
                    "ready": bool(status.get("ready")),
                    "status": status,
                }
            else:
                result = capture_ready_recording(client, config, lang=args.lang)
                touch_pet_events = touch_pet_tracker.observe(result.get("status", {}))
                if result.get("ready") and result.get("consumed"):
                    consumed += 1
                    recording_source = recording_source_from_result(result)
                    event = {
                        "type": "transcript",
                        "source": "stackchan_touch" if recording_source == "touch" else "stackchan_mic",
                        "recording_source": recording_source,
                        "timestamp": utc_now(),
                        "lang": args.lang,
                        "text": result.get("text", ""),
                        "duration": result.get("duration", 0),
                        "detected_language": result.get("language", "?"),
                        "audio_bytes": result.get("audio_bytes", 0),
                        "wav_path": result.get("wav_path"),
                    }
                    if inbox_path is not None and should_append_to_inbox(event):
                        append_event(event, inbox_path)
                    frontend = forward_event_to_frontend(event, args)
                    if frontend is not None:
                        event["frontend"] = frontend
                elif args.verbose_idle or args.once:
                    event = {
                        "type": "idle",
                        "timestamp": utc_now(),
                        "ready": bool(result.get("ready")),
                        "consumed": bool(result.get("consumed")),
                        "error": result.get("error"),
                        "status": result.get("status", {}),
                    }
                else:
                    event = None

            if event is not None:
                print_event(event)

            for _ in range(touch_pet_events):
                touch_event = build_touch_pet_event()
                frontend = forward_event_to_frontend(touch_event, args)
                if frontend is not None:
                    touch_event["frontend"] = frontend
                print_event(touch_event)

            if args.once or (args.max_events and consumed >= args.max_events):
                return 0
            time.sleep(max(0.1, args.interval))
        except KeyboardInterrupt:
            print_event({"type": "stop", "timestamp": utc_now(), "reason": "keyboard_interrupt"})
            return 0
        except Exception as exc:
            print_event({"type": "error", "timestamp": utc_now(), "error": str(exc)})
            if args.once:
                return 1
            time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
