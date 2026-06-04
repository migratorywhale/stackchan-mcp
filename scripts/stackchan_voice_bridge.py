#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def print_event(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def should_append_to_inbox(event: dict) -> bool:
    return event.get("type") == "transcript" and bool(str(event.get("text") or "").strip())


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
    return parser


def main() -> int:
    from mcp_server.listening import capture_ready_recording
    from mcp_server.stackchan_client import StackchanClient
    from mcp_server.stackchan_config import load_config
    from mcp_server.voice_inbox import append_event, resolve_inbox_path

    args = build_parser().parse_args()
    load_env_file(REPO_ROOT / ".env")
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
