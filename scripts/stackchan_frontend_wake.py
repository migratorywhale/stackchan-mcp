from __future__ import annotations

import time
from typing import Any

import requests

from mcp_server.telemetry import emit_event, new_request_id

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
    source: str = "",
    request_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    request_id = request_id or str(event.get("request_id") or "") or new_request_id()
    text = str(event.get("text") or "").strip()
    if not text:
        result = {"ok": False, "skipped": "empty transcript", "timing_ms": {"wake_total": 0}}
        emit_event(
            "stackchan.voice.wake.skipped",
            body="Wake forwarding skipped: empty transcript",
            request_id=request_id,
            attributes={"stackchan.wake.skipped": "empty transcript"},
        )
        return result
    matched, prompt_text, matched_word = match_wake_word(text, wake_words)
    if not matched:
        result = {
            "ok": False,
            "skipped": "wake word not found",
            "wake_words": list(wake_words),
            "timing_ms": {"wake_total": round((time.perf_counter() - started) * 1000)},
        }
        emit_event(
            "stackchan.voice.wake.skipped",
            body="Wake forwarding skipped: wake word not found",
            request_id=request_id,
            attributes={
                "stackchan.wake.skipped": "wake word not found",
                "stackchan.latency.wake_total_ms": result["timing_ms"]["wake_total"],
            },
        )
        return result
    if not wake_url or not session_id:
        result = {
            "ok": False,
            "skipped": "frontend wake not configured",
            "timing_ms": {"wake_total": round((time.perf_counter() - started) * 1000)},
        }
        emit_event(
            "stackchan.voice.wake.skipped",
            body="Wake forwarding skipped: frontend wake not configured",
            request_id=request_id,
            attributes={
                "stackchan.wake.skipped": "frontend wake not configured",
                "stackchan.latency.wake_total_ms": result["timing_ms"]["wake_total"],
            },
        )
        return result

    payload: dict[str, Any] = {
        "session_id": session_id,
        "prompt": frontend_prompt(prompt_text, prompt_prefix),
        "force": force,
        "quiet_minutes": quiet_minutes,
    }
    if source:
        payload["source"] = source
    if model:
        payload["model"] = model

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempts = max(0, int(retries)) + 1
    last_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        post_started = time.perf_counter()
        try:
            response = requests.post(wake_url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            result = {
                "ok": False,
                "attempts": attempt,
                "error": str(exc),
                "timing_ms": {
                    "wake_post": round((time.perf_counter() - post_started) * 1000),
                    "wake_total": round((time.perf_counter() - started) * 1000),
                },
            }
            emit_event(
                "stackchan.voice.wake.failed",
                body="Wake forwarding request failed",
                severity_text="ERROR",
                request_id=request_id,
                attributes={
                    "stackchan.error": str(exc),
                    "stackchan.wake.attempts": attempt,
                    "stackchan.latency.wake_post_ms": result["timing_ms"]["wake_post"],
                    "stackchan.latency.wake_total_ms": result["timing_ms"]["wake_total"],
                },
            )
            return result

        result: dict[str, Any] = {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "attempts": attempt,
            "timing_ms": {
                "wake_post": round((time.perf_counter() - post_started) * 1000),
                "wake_total": round((time.perf_counter() - started) * 1000),
            },
        }
        try:
            result["body"] = response.json()
        except ValueError:
            result["body"] = response.text[:500]
        if result["ok"]:
            if matched_word:
                result["wake_word"] = matched_word
            emit_event(
                "stackchan.voice.wake.forwarded",
                body="Wake forwarded to frontend",
                request_id=request_id,
                attributes={
                    "stackchan.wake.word": matched_word,
                    "stackchan.wake.attempts": attempt,
                    "http.response.status_code": response.status_code,
                    "stackchan.latency.wake_post_ms": result["timing_ms"]["wake_post"],
                    "stackchan.latency.wake_total_ms": result["timing_ms"]["wake_total"],
                },
            )
            return result
        last_result = result
        if response.status_code != 409 or attempt >= attempts:
            break
        time.sleep(max(0.0, retry_delay))

    result = last_result or {"ok": False, "error": "frontend wake failed without response"}
    if matched_word:
        result["wake_word"] = matched_word
    emit_event(
        "stackchan.voice.wake.failed",
        body="Wake forwarding was rejected by frontend",
        severity_text="WARN",
        request_id=request_id,
        attributes={
            "stackchan.wake.word": matched_word,
            "stackchan.wake.attempts": result.get("attempts"),
            "http.response.status_code": result.get("status_code"),
            "stackchan.latency.wake_total_ms": result.get("timing_ms", {}).get("wake_total"),
        },
    )
    return result
