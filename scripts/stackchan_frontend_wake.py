from __future__ import annotations

import time
from typing import Any

import requests

DEFAULT_PROMPT_PREFIX = "[Stack-chan语音输入] "
LEADING_FILLERS = (
    "好的",
    "好",
    "嗯嗯",
    "嗯",
    "啊",
    "呀",
    "诶",
    "欸",
    "那个",
)


def frontend_prompt(text: str, prefix: str = DEFAULT_PROMPT_PREFIX) -> str:
    return f"{prefix}{text.strip()}"


def parse_wake_words(raw: str) -> tuple[str, ...]:
    return tuple(word.strip() for word in raw.split(",") if word.strip())


def strip_leading_fillers(text: str) -> str:
    remaining = text.strip()
    changed = True
    while changed:
        changed = False
        remaining = remaining.lstrip(" ，,。.!！?？:：、…")
        for filler in LEADING_FILLERS:
            if remaining.startswith(filler):
                remaining = remaining[len(filler) :]
                changed = True
                break
    return remaining.lstrip(" ，,。.!！?？:：、…")


def wake_word_matches_start(text: str, word: str) -> bool:
    if text.startswith(word):
        return True
    duplicated_first = f"{word[0]}{word}" if word else ""
    return bool(duplicated_first and text.startswith(duplicated_first))


def match_wake_word(text: str, wake_words: tuple[str, ...]) -> tuple[bool, str, str]:
    clean_text = text.strip()
    if not wake_words:
        return True, clean_text, ""
    match_text = strip_leading_fillers(clean_text)

    for word in wake_words:
        if not word:
            continue
        if wake_word_matches_start(match_text, word):
            return True, clean_text, word
        marker = f" {word}"
        if marker in clean_text:
            before, after = clean_text.split(marker, 1)
            if not before.strip():
                return True, clean_text, word
    return False, clean_text, ""


def forward_to_frontend(
    event: dict[str, Any],
    *,
    wake_url: str,
    session_id: str,
    token: str = "",
    model: str = "",
    timeout: float = 10.0,
    retries: int = 0,
    retry_delay: float = 3.0,
    force: bool = True,
    quiet_minutes: int = 0,
    prompt_prefix: str = DEFAULT_PROMPT_PREFIX,
    wake_words: tuple[str, ...] = (),
) -> dict[str, Any]:
    text = str(event.get("text") or "").strip()
    if not text:
        return {"ok": False, "skipped": "empty transcript"}
    matched, prompt_text, matched_word = match_wake_word(text, wake_words)
    if not matched:
        return {"ok": False, "skipped": "wake word not found", "wake_words": list(wake_words)}
    if not wake_url or not session_id:
        return {"ok": False, "skipped": "frontend wake not configured"}

    payload: dict[str, Any] = {
        "session_id": session_id,
        "prompt": frontend_prompt(prompt_text, prompt_prefix),
        "display_prompt": prompt_text,
        "source": "stackchan_voice",
        "force": force,
        "quiet_minutes": quiet_minutes,
    }
    if model:
        payload["model"] = model

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempts = max(0, int(retries)) + 1
    last_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(wake_url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            return {"ok": False, "attempts": attempt, "error": str(exc)}

        result: dict[str, Any] = {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "attempts": attempt,
        }
        try:
            result["body"] = response.json()
        except ValueError:
            result["body"] = response.text[:500]
        if result["ok"]:
            if matched_word:
                result["wake_word"] = matched_word
            return result
        last_result = result
        if response.status_code != 409 or attempt >= attempts:
            break
        time.sleep(max(0.0, retry_delay))

    result = last_result or {"ok": False, "error": "frontend wake failed without response"}
    if matched_word:
        result["wake_word"] = matched_word
    return result
