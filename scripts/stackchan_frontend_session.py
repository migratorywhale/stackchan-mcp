#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class SessionResolutionError(RuntimeError):
    pass


def default_registry_path() -> Path:
    return Path(os.environ.get("STACKCHAN_FRONTEND_REGISTRY", "web-sessions.json"))


def load_sessions(path: str | Path | None = None) -> list[dict[str, Any]]:
    registry_path = Path(path) if path is not None else default_registry_path()
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SessionResolutionError(f"session registry not found: {registry_path}") from None
    except json.JSONDecodeError as exc:
        raise SessionResolutionError(f"session registry is not valid JSON: {registry_path}: {exc}") from None
    if not isinstance(raw, list):
        raise SessionResolutionError(f"session registry must be a JSON array: {registry_path}")
    return [item for item in raw if isinstance(item, dict)]


def is_archived(session: dict[str, Any]) -> bool:
    return bool(session.get("archived"))


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        return datetime.min
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min


def select_session(
    sessions: list[dict[str, Any]],
    *,
    title: str = "",
    include_archived: bool = False,
) -> dict[str, Any] | None:
    candidates = [s for s in sessions if include_archived or not is_archived(s)]
    if title:
        title_norm = title.casefold()
        candidates = [
            s
            for s in candidates
            if title_norm in str(s.get("title") or "").casefold()
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: parse_time(s.get("last") or s.get("created")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve the frontend session Stack-chan should wake."
    )
    parser.add_argument("--registry", default=None, help="web-sessions.json path")
    parser.add_argument("--title", default="", help="Optional title substring, e.g. lab-room")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the selected session object")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        session = select_session(
            load_sessions(args.registry),
            title=args.title,
            include_archived=args.include_archived,
        )
    except SessionResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not session:
        print("no matching frontend session", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(session, ensure_ascii=False))
    else:
        print(session.get("id") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
