import json
import os
from pathlib import Path
from typing import Any

from .audio_server import AUDIO_DIR

DEFAULT_INBOX_PATH = AUDIO_DIR / "voice_inbox.jsonl"


def resolve_inbox_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("STACKCHAN_VOICE_INBOX", DEFAULT_INBOX_PATH))


def append_event(event: dict[str, Any], path: str | Path | None = None) -> Path:
    inbox_path = resolve_inbox_path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with inbox_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return inbox_path


def read_events(limit: int = 10, path: str | Path | None = None) -> list[dict[str, Any]]:
    inbox_path = resolve_inbox_path(path)
    if not inbox_path.exists():
        return []

    events = []
    for line in inbox_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    safe_limit = max(1, min(int(limit), 50))
    return events[-safe_limit:]


def clear_events(path: str | Path | None = None) -> None:
    inbox_path = resolve_inbox_path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text("", encoding="utf-8")


def format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No Stack-chan voice transcripts in inbox."

    lines = []
    for index, event in enumerate(events, start=1):
        text = event.get("text") or ""
        timestamp = event.get("timestamp") or "unknown-time"
        duration = event.get("duration", "?")
        language = event.get("detected_language") or event.get("language") or "?"
        wav_path = event.get("wav_path") or "?"
        lines.append(f"{index}. [{timestamp}] ({duration}s, {language}) {text}\n   wav: {wav_path}")
    return "\n".join(lines)
