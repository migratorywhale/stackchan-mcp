#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.stackchan_frontend_session import load_sessions, select_session  # noqa: E402
from scripts.stackchan_frontend_wake import (  # noqa: E402
    DEFAULT_PROMPT_PREFIX,
    forward_to_frontend,
    parse_wake_words,
)


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


def load_frontend_token() -> None:
    if os.environ.get("STACKCHAN_FRONTEND_TOKEN"):
        return
    env_path_raw = os.environ.get("STACKCHAN_FRONTEND_ENV", "")
    if not env_path_raw:
        return
    env_path = Path(env_path_raw)
    if not env_path.exists():
        return

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
            os.environ["STACKCHAN_FRONTEND_TOKEN"] = value
        return


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def print_event(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def should_append_to_inbox(event: dict) -> bool:
    return event.get("type") == "transcript" and bool(str(event.get("text") or "").strip())


def resolve_wake_session(session_id: str, title: str = "") -> str:
    if title:
        session = select_session(load_sessions(), title=title)
        if not session:
            raise SystemExit(f"no matching frontend session title: {title}")
        return str(session.get("id") or "")
    if session_id in {"latest", "auto"}:
        session = select_session(load_sessions())
        if not session:
            raise SystemExit("no non-archived frontend session found")
        return str(session.get("id") or "")
    return session_id


def forward_event_to_frontend(event: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    wake_session_id = resolve_wake_session(args.wake_session_id, args.wake_session_title)
    wake_url = args.wake_url
    if not wake_url and wake_session_id:
        wake_url = "http://127.0.0.1:3200/wake"
    if not wake_url and not wake_session_id:
        return None
    return forward_to_frontend(
        event,
        wake_url=wake_url,
        session_id=wake_session_id,
        token=args.wake_token,
        model=args.wake_model,
        timeout=args.wake_timeout,
        retries=args.wake_retries,
        retry_delay=args.wake_retry_delay,
        force=not args.wake_no_force,
        quiet_minutes=args.wake_quiet_minutes,
        prompt_prefix=args.prompt_prefix,
        wake_words=parse_wake_words(args.wake_words),
        source=str(event.get("source") or "stackchan_mic"),
    )


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
    load_frontend_token()
    args = build_parser().parse_args()
    config = load_config()
    client = StackchanClient(config)
    consumed = 0
    inbox_path = None if args.no_inbox else resolve_inbox_path(args.inbox)

    while True:
        try:
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
                if result.get("ready") and result.get("consumed"):
                    consumed += 1
                    event = {
                        "type": "transcript",
                        "source": "stackchan_mic",
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
