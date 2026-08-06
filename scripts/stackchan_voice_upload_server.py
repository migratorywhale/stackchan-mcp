#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_server import audio_processing  # noqa: E402
from mcp_server.audio_server import AUDIO_DIR  # noqa: E402
from mcp_server.stackchan_config import StackchanConfig, load_config  # noqa: E402
from mcp_server.telemetry import emit_event, new_request_id  # noqa: E402
from mcp_server.voice_inbox import append_event, resolve_inbox_path  # noqa: E402
from scripts.stackchan_frontend_session import SessionResolutionError  # noqa: E402
from scripts.stackchan_frontend_wake import (  # noqa: E402
    forward_to_frontend,
    parse_wake_words,
)
from scripts.stackchan_voice_bridge import (  # noqa: E402
    load_env_file,
    load_frontend_token,
    resolve_wake_session,
    should_append_to_inbox,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_UPLOAD_RATE_PER_MINUTE = 12
DEFAULT_UPLOAD_RATE_WINDOW_SECONDS = 60.0
DEFAULT_UPLOAD_PROMPT_PREFIX = "[前端语音输入] "
TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^\s&]+")


@dataclass(frozen=True)
class ServerOptions:
    lang: str
    max_bytes: int
    inbox_path: Path | None
    wake_url: str
    wake_session_id: str
    wake_session_title: str
    wake_token: str
    wake_model: str
    wake_timeout: float
    wake_retries: int
    wake_retry_delay: float
    wake_force: bool
    wake_quiet_minutes: int
    prompt_prefix: str
    wake_words: tuple[str, ...]
    upload_token: str
    upload_rate_per_minute: int
    upload_rate_window_seconds: float
    allowed_origins: tuple[str, ...]


class UploadRateLimiter:
    def __init__(self, limit_per_minute: int, *, window_seconds: float = DEFAULT_UPLOAD_RATE_WINDOW_SECONDS):
        self.limit_per_minute = limit_per_minute
        self.window_seconds = max(1.0, window_seconds)
        self._attempts: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, client_id: str, *, now: float | None = None) -> bool:
        if self.limit_per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        window_start = now - self.window_seconds
        with self._lock:
            attempts = self._attempts[client_id]
            while attempts and attempts[0] < window_start:
                attempts.popleft()
            if len(attempts) >= self.limit_per_minute:
                return False
            attempts.append(now)
            return True


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    send_json_headers(handler, status, len(body))
    handler.wfile.write(body)


def send_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    origin = handler.headers.get("Origin", "")
    server = cast(VoiceUploadServer, handler.server)
    allowed = server.options.allowed_origins
    if origin and origin in allowed:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")


def send_json_headers(handler: BaseHTTPRequestHandler, status: int, content_length: int) -> None:
    handler.send_response(status)
    send_cors_headers(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(content_length))
    handler.end_headers()


def json_content_length(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def write_html(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    send_html_headers(handler, status, len(body))
    handler.wfile.write(body)


def send_html_headers(handler: BaseHTTPRequestHandler, status: int, content_length: int) -> None:
    handler.send_response(status)
    send_cors_headers(handler)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(content_length))
    handler.end_headers()


def build_recorder_page(options: ServerOptions) -> str:
    wake_words = " / ".join(html.escape(word) for word in options.wake_words) if options.wake_words else "未启用"
    frontend = "enabled" if options.wake_url and options.wake_session_id else "inbox only"
    wake_hint = (
        f"说话开头带 {wake_words} 之一，才会转发到前端；不带唤醒词的录音只进本地 inbox。"
        if options.wake_words
        else "当前未配置唤醒词；录音会先进入本地 inbox，只有配置 frontend 和唤醒规则后才会转发。"
    )
    upload_path = "/voice/upload"
    token_block = ""
    if options.upload_token:
        token_block = """
    <label class="token">上传 token
      <input id="upload-token" type="password" autocomplete="off" placeholder="输入本机 .env 里的上传 token">
    </label>
    <p class="hint">token 只保存在这个浏览器标签页的 sessionStorage；旧链接里的 ?token= 仍会迁移到这里并清理地址栏，但这种 URL 传 token 的方式已弃用。</p>
"""
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stack-chan Voice Upload</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6efe4;
      --ink: #3b332b;
      --muted: #8d7c6d;
      --line: #dccbb5;
      --accent: #2e8b57;
      --danger: #b25a42;
      --card: #fffaf2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    main {{
      width: min(720px, 100%);
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
      box-shadow: 0 12px 30px rgba(60, 42, 24, 0.08);
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
    p {{ line-height: 1.6; }}
    .meta {{ color: var(--muted); font-size: 14px; margin-top: 0; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 22px 0; }}
    label.token {{
      display: block;
      margin: 18px 0;
      color: var(--muted);
      font-size: 14px;
    }}
    label.token input {{
      display: block;
      width: 100%;
      margin-top: 8px;
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      border-radius: 8px;
      padding: 12px 14px;
      font-size: 16px;
    }}
    button, label.file {{
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 16px;
      cursor: pointer;
    }}
    button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
    button.danger {{ background: var(--danger); color: white; border-color: var(--danger); }}
    button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
    input[type="file"] {{ display: none; }}
    pre {{
      min-height: 120px;
      white-space: pre-wrap;
      word-break: break-word;
      background: #f2eadf;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      font-size: 14px;
    }}
    .hint {{ font-size: 14px; color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <h1>Stack-chan Voice Upload</h1>
    <p class="meta">frontend: {frontend} · wake words: {wake_words}</p>
    <p>{wake_hint}</p>
{token_block}
    <div class="controls">
      <button id="start" class="primary">开始录音</button>
      <button id="stop" class="danger" disabled>停止并发送</button>
      <label class="file">上传音频文件<input id="file" type="file" accept="audio/*" capture></label>
    </div>
    <p class="hint">如果手机浏览器因为 HTTP 禁止麦克风，请用“上传音频文件”。直接录音会在浏览器里编码成 WAV 再发送。</p>
    <pre id="log">Ready.</pre>
  </main>
  <script>
    const log = document.getElementById('log');
    const startBtn = document.getElementById('start');
    const stopBtn = document.getElementById('stop');
    const fileInput = document.getElementById('file');
    const tokenInput = document.getElementById('upload-token');
    const tokenStorageKey = 'stackchan_voice_upload_token';
    let audioContext, stream, source, processor, chunks, sampleRate;

    function say(message, data) {{
      log.textContent = data ? message + "\\n" + JSON.stringify(data, null, 2) : message;
    }}

    function initializeTokenInput() {{
      const query = new URLSearchParams(window.location.search);
      const tokenFromUrl = query.get('token') || '';
      if (tokenFromUrl) {{
        sessionStorage.setItem(tokenStorageKey, tokenFromUrl);
        query.delete('token');
        const cleanQuery = query.toString();
        const cleanUrl = window.location.pathname + (cleanQuery ? '?' + cleanQuery : '') + window.location.hash;
        window.history.replaceState(null, '', cleanUrl);
      }}
      if (!tokenInput) return;
      tokenInput.value = sessionStorage.getItem(tokenStorageKey) || '';
      tokenInput.addEventListener('input', () => {{
        const value = tokenInput.value.trim();
        if (value) {{
          sessionStorage.setItem(tokenStorageKey, value);
        }} else {{
          sessionStorage.removeItem(tokenStorageKey);
        }}
      }});
    }}

    function uploadHeaders(blob) {{
      const headers = {{ 'Content-Type': blob.type || 'audio/wav' }};
      const token = (tokenInput?.value || sessionStorage.getItem(tokenStorageKey) || '').trim();
      if (token) headers['X-Stackchan-Upload-Token'] = token;
      return headers;
    }}

    function uploadUrl() {{
      return '{upload_path}';
    }}

    async function postAudio(blob) {{
      say('Uploading...');
      const response = await fetch(uploadUrl(), {{
        method: 'POST',
        headers: uploadHeaders(blob),
        body: blob,
      }});
      const data = await response.json().catch(() => ({{ ok: false, error: 'non-json response' }}));
      if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
      say('Done.', data);
    }}

    function encodeWav(buffers, rate) {{
      const length = buffers.reduce((n, b) => n + b.length, 0);
      const data = new Float32Array(length);
      let offset = 0;
      for (const buffer of buffers) {{
        data.set(buffer, offset);
        offset += buffer.length;
      }}
      const wav = new ArrayBuffer(44 + data.length * 2);
      const view = new DataView(wav);
      writeString(view, 0, 'RIFF');
      view.setUint32(4, 36 + data.length * 2, true);
      writeString(view, 8, 'WAVE');
      writeString(view, 12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, rate, true);
      view.setUint32(28, rate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeString(view, 36, 'data');
      view.setUint32(40, data.length * 2, true);
      let pos = 44;
      for (let i = 0; i < data.length; i++) {{
        const sample = Math.max(-1, Math.min(1, data[i]));
        view.setInt16(pos, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
        pos += 2;
      }}
      return new Blob([view], {{ type: 'audio/wav' }});
    }}

    function writeString(view, offset, value) {{
      for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
    }}

    startBtn.addEventListener('click', async () => {{
      try {{
        stream = await navigator.mediaDevices.getUserMedia({{ audio: true }});
        audioContext = new AudioContext();
        sampleRate = audioContext.sampleRate;
        source = audioContext.createMediaStreamSource(stream);
        processor = audioContext.createScriptProcessor(4096, 1, 1);
        chunks = [];
        processor.onaudioprocess = event => {{
          chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        }};
        source.connect(processor);
        processor.connect(audioContext.destination);
        startBtn.disabled = true;
        stopBtn.disabled = false;
        say('Recording...');
      }} catch (error) {{
        say('Mic unavailable: ' + error.message);
      }}
    }});

    stopBtn.addEventListener('click', async () => {{
      try {{
        startBtn.disabled = false;
        stopBtn.disabled = true;
        processor?.disconnect();
        source?.disconnect();
        stream?.getTracks().forEach(track => track.stop());
        const blob = encodeWav(chunks || [], sampleRate || 48000);
        await audioContext?.close();
        await postAudio(blob);
      }} catch (error) {{
        say('Upload failed: ' + error.message);
      }}
    }});

    fileInput.addEventListener('change', async () => {{
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      try {{
        await postAudio(file);
      }} catch (error) {{
        say('Upload failed: ' + error.message);
      }} finally {{
        fileInput.value = '';
      }}
    }});

    initializeTokenInput();
  </script>
</body>
</html>"""


def save_uploaded_wav(audio_data: bytes, audio_dir: Path = AUDIO_DIR) -> Path:
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav_path = audio_dir / f"upload_{time.time_ns()}.wav"
    wav_path.write_bytes(audio_data)
    return wav_path


def build_transcript_event(
    *,
    wav_path: Path,
    audio_bytes: int,
    asr_result: dict[str, Any],
    lang: str,
    request_id: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "transcript",
        "source": "voice_upload",
        "request_id": request_id,
        "timestamp": timestamp or utc_now(),
        "lang": lang,
        "text": asr_result.get("text", ""),
        "duration": asr_result.get("duration", 0),
        "detected_language": asr_result.get("language", "?"),
        "audio_bytes": audio_bytes,
        "wav_path": str(wav_path),
    }


def process_uploaded_wav(
    audio_data: bytes,
    config: StackchanConfig,
    *,
    lang: str = "zh",
    audio_dir: Path = AUDIO_DIR,
    transcribe_fn=audio_processing.transcribe_audio,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or new_request_id()
    started = time.perf_counter()
    if not config.fish_audio_key:
        raise RuntimeError("Fish Audio key is not configured; set FISH_AUDIO_KEY before uploading audio.")

    save_started = time.perf_counter()
    wav_path = save_uploaded_wav(audio_data, audio_dir)
    save_ms = round((time.perf_counter() - save_started) * 1000)
    asr_started = time.perf_counter()
    asr_result = transcribe_fn(wav_path, lang, config)
    event = build_transcript_event(
        wav_path=wav_path,
        audio_bytes=len(audio_data),
        asr_result=asr_result,
        lang=lang,
        request_id=request_id,
    )
    event["timing_ms"] = {
        "upload_save": save_ms,
        "asr": round((time.perf_counter() - asr_started) * 1000),
        "process_total": round((time.perf_counter() - started) * 1000),
    }
    emit_event(
        "stackchan.voice.asr.completed",
        body="Uploaded voice recording transcribed",
        request_id=request_id,
        attributes={
            "stackchan.lang": lang,
            "stackchan.audio.bytes": len(audio_data),
            "stackchan.transcript.length": len(str(event.get("text") or "")),
            "stackchan.latency.upload_save_ms": event["timing_ms"]["upload_save"],
            "stackchan.latency.asr_ms": event["timing_ms"]["asr"],
            "stackchan.latency.process_total_ms": event["timing_ms"]["process_total"],
        },
    )
    return event


def is_upload_authorized(path: str, headers: Any, token: str) -> bool:
    if not token:
        return True
    url = urlparse(path)
    query_token = parse_qs(url.query).get("token", [""])[0]
    auth = headers.get("Authorization", "")
    header_token = headers.get("X-Stackchan-Upload-Token", "")
    # `?token=` remains accepted for existing bookmarked URLs, but new clients
    # should prefer the header-based flow so tokens do not linger in URLs.
    return query_token == token or auth == f"Bearer {token}" or header_token == token


def build_health_payload(options: ServerOptions) -> dict[str, Any]:
    return {
        "ok": True,
        "service": "stackchan_voice_upload_server",
        "frontend_enabled": bool(options.wake_url and options.wake_session_id),
        "inbox_enabled": options.inbox_path is not None,
        "config": {
            "max_bytes": options.max_bytes,
            "wake_timeout": options.wake_timeout,
            "wake_retries": options.wake_retries,
            "wake_retry_delay": options.wake_retry_delay,
            "wake_force": options.wake_force,
            "wake_quiet_minutes": options.wake_quiet_minutes,
            "upload_rate_per_minute": options.upload_rate_per_minute,
            "upload_rate_window_seconds": options.upload_rate_window_seconds,
            "upload_token_configured": bool(options.upload_token),
        },
    }


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_upload_token_requirement(host: str, upload_token: str, parser: argparse.ArgumentParser) -> None:
    if upload_token or is_loopback_host(host):
        return
    parser.error(
        "Refusing to bind the voice upload server to a non-loopback host without "
        "STACKCHAN_VOICE_UPLOAD_TOKEN / --upload-token. Use 127.0.0.1 for local-only access."
    )


def resolve_frontend_wake_session(options: ServerOptions) -> str:
    return resolve_wake_session(options.wake_session_id, options.wake_session_title)


def write_rate_limit_error(handler: BaseHTTPRequestHandler) -> None:
    body = b'{"ok":false,"error":"rate limit exceeded"}'
    handler.send_response(429)
    send_cors_headers(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    server = cast(VoiceUploadServer, handler.server)
    handler.send_header("Retry-After", str(round(server.options.upload_rate_window_seconds)))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class VoiceUploadServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, *, config: StackchanConfig, options: ServerOptions):
        super().__init__(server_address, handler_class)
        self.config = config
        self.options = options
        self.rate_limiter = UploadRateLimiter(
            options.upload_rate_per_minute,
            window_seconds=options.upload_rate_window_seconds,
        )


class VoiceUploadHandler(BaseHTTPRequestHandler):
    @property
    def voice_server(self) -> VoiceUploadServer:
        return cast(VoiceUploadServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:
        message = TOKEN_QUERY_RE.sub(r"\1<redacted>", format % args)
        print(f"[voice-upload] {self.address_string()} - {message}", flush=True)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path == "/recorder":
            write_html(self, 200, build_recorder_page(self.voice_server.options))
            return
        if path == "/health":
            write_json(self, 200, build_health_payload(self.voice_server.options))
            return
        write_json(self, 404, {"ok": False, "error": "not found"})

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path == "/recorder":
            send_html_headers(
                self,
                200,
                len(build_recorder_page(self.voice_server.options).encode("utf-8")),
            )
            return
        if path == "/health":
            send_json_headers(
                self,
                200,
                json_content_length(build_health_payload(self.voice_server.options)),
            )
            return
        send_json_headers(self, 404, json_content_length({"ok": False, "error": "not found"}))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        send_cors_headers(self)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Stackchan-Upload-Token")
        self.end_headers()

    def do_POST(self) -> None:
        request_id = new_request_id()
        request_started = time.perf_counter()
        path = urlparse(self.path).path
        if path != "/voice/upload":
            write_json(self, 404, {"ok": False, "error": "not found"})
            return
        if not self.voice_server.rate_limiter.allow(self.client_address[0]):
            write_rate_limit_error(self)
            return
        if not self.is_upload_authorized():
            write_json(self, 401, {"ok": False, "error": "unauthorized"})
            return

        if not self.voice_server.config.fish_audio_key:
            write_json(
                self,
                503,
                {
                    "ok": False,
                    "error": "Fish Audio key is not configured; set FISH_AUDIO_KEY before uploading audio.",
                },
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            write_json(self, 400, {"ok": False, "error": "invalid Content-Length"})
            return
        if content_length <= 0:
            write_json(self, 400, {"ok": False, "error": "empty audio body"})
            return
        if content_length > self.voice_server.options.max_bytes:
            write_json(self, 413, {"ok": False, "error": "audio payload too large"})
            return

        read_started = time.perf_counter()
        audio_data = self.rfile.read(content_length)
        read_ms = round((time.perf_counter() - read_started) * 1000)
        if len(audio_data) != content_length:
            write_json(self, 400, {"ok": False, "error": "short audio body"})
            return

        try:
            event = process_uploaded_wav(
                audio_data,
                self.voice_server.config,
                lang=self.voice_server.options.lang,
                request_id=request_id,
            )
        except Exception as exc:
            write_json(self, 500, {"ok": False, "error": str(exc)})
            return

        inbox_path = self.voice_server.options.inbox_path
        appended_to_inbox = False
        if inbox_path is not None and should_append_to_inbox(event):
            inbox_started = time.perf_counter()
            append_event(event, inbox_path)
            inbox_ms = round((time.perf_counter() - inbox_started) * 1000)
            appended_to_inbox = True
        else:
            inbox_ms = 0

        frontend_started = time.perf_counter()
        try:
            wake_session_id = resolve_frontend_wake_session(self.voice_server.options)
        except SessionResolutionError as exc:
            wake_session_id = ""
            frontend = {
                "ok": False,
                "skipped": f"frontend session not resolved: {exc}",
                "timing_ms": {"wake_total": 0},
            }
            logger.warning(
                "frontend wake session unresolved; skipping wake forward, upload still recorded: %s", exc
            )
        else:
            frontend = forward_to_frontend(
                event,
                wake_url=self.voice_server.options.wake_url,
                session_id=wake_session_id,
                token=self.voice_server.options.wake_token,
                model=self.voice_server.options.wake_model,
                timeout=self.voice_server.options.wake_timeout,
                retries=self.voice_server.options.wake_retries,
                retry_delay=self.voice_server.options.wake_retry_delay,
                force=self.voice_server.options.wake_force,
                quiet_minutes=self.voice_server.options.wake_quiet_minutes,
                prompt_prefix=self.voice_server.options.prompt_prefix,
                wake_words=self.voice_server.options.wake_words,
                source=str(event.get("source") or "voice_upload"),
                request_id=request_id,
            )
        frontend_ms = round((time.perf_counter() - frontend_started) * 1000)
        timing_ms = {
            "upload_read": read_ms,
            "inbox": inbox_ms,
            "frontend": frontend_ms,
            "request_total": round((time.perf_counter() - request_started) * 1000),
        }
        emit_event(
            "stackchan.voice.upload.completed",
            body="Voice upload request completed",
            request_id=request_id,
            attributes={
                "stackchan.audio.bytes": len(audio_data),
                "stackchan.inbox.appended": appended_to_inbox,
                "stackchan.frontend.ok": frontend.get("ok"),
                "stackchan.latency.upload_read_ms": timing_ms["upload_read"],
                "stackchan.latency.inbox_ms": timing_ms["inbox"],
                "stackchan.latency.frontend_ms": timing_ms["frontend"],
                "stackchan.latency.request_total_ms": timing_ms["request_total"],
            },
        )

        write_json(
            self,
            200,
            {
                "ok": True,
                "event": event,
                "inbox_appended": appended_to_inbox,
                "frontend": frontend,
                "timing_ms": timing_ms,
            },
        )

    def is_upload_authorized(self) -> bool:
        return is_upload_authorized(self.path, self.headers, self.voice_server.options.upload_token)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Receive Stack-chan pushed WAV recordings at /voice/upload, transcribe them, "
            "write the local voice inbox, and optionally forward text into a frontend /wake endpoint."
        )
    )
    parser.add_argument("--host", default=os.environ.get("STACKCHAN_VOICE_UPLOAD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STACKCHAN_VOICE_UPLOAD_PORT", "8767")),
    )
    parser.add_argument("--lang", default=os.environ.get("STACKCHAN_VOICE_LANG", "zh"))
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=int(os.environ.get("STACKCHAN_VOICE_UPLOAD_MAX_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
    )
    parser.add_argument(
        "--inbox",
        default=os.environ.get("STACKCHAN_VOICE_INBOX"),
        help="JSONL inbox path. Default: /tmp/stackchan_audio/voice_inbox.jsonl",
    )
    parser.add_argument("--no-inbox", action="store_true", help="Do not append transcripts to inbox")
    parser.add_argument(
        "--wake-url",
        default=os.environ.get("STACKCHAN_FRONTEND_WAKE_URL", ""),
        help="agent-host /wake URL. If omitted, frontend forwarding is skipped.",
    )
    parser.add_argument(
        "--wake-session-id",
        default=os.environ.get("STACKCHAN_FRONTEND_SESSION_ID", ""),
        help="Frontend session UUID to receive transcripts.",
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
        help="Seconds between 409 busy retries. Default: 3.",
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
        default=os.environ.get(
            "STACKCHAN_VOICE_UPLOAD_PROMPT_PREFIX",
            os.environ.get("STACKCHAN_FRONTEND_PROMPT_PREFIX", DEFAULT_UPLOAD_PROMPT_PREFIX),
        ),
    )
    parser.add_argument(
        "--wake-words",
        default=os.environ.get("STACKCHAN_VOICE_WAKE_WORDS", ""),
        help=(
            "Comma-separated activation words. If set, frontend forwarding only happens when "
            "the transcript starts with one of these words; inbox logging still happens."
        ),
    )
    parser.add_argument(
        "--upload-token",
        default=os.environ.get("STACKCHAN_VOICE_UPLOAD_TOKEN", ""),
        help=(
            "Optional token required for POST /voice/upload. The recorder page sends it as "
            "X-Stackchan-Upload-Token; ?token=... is accepted only for backward compatibility."
        ),
    )
    parser.add_argument(
        "--upload-rate-per-minute",
        type=int,
        default=int(os.environ.get("STACKCHAN_VOICE_UPLOAD_RATE_PER_MINUTE", str(DEFAULT_UPLOAD_RATE_PER_MINUTE))),
        help="Maximum POST /voice/upload attempts per client IP per minute. Set 0 to disable.",
    )
    parser.add_argument(
        "--upload-rate-window-seconds",
        type=float,
        default=float(
            os.environ.get("STACKCHAN_VOICE_UPLOAD_RATE_WINDOW_SECONDS", str(DEFAULT_UPLOAD_RATE_WINDOW_SECONDS))
        ),
        help="Rate-limit window in seconds. Default: 60.",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="Allowed browser Origin for cross-origin upload requests. Repeatable. Same-origin use does not need this.",
    )
    return parser


def main() -> int:
    load_env_file(REPO_ROOT / ".env")
    load_frontend_token()
    parser = build_parser()
    args = parser.parse_args()
    config = load_config()
    wake_url = args.wake_url
    if not wake_url and args.wake_session_id:
        wake_url = "http://127.0.0.1:3200/wake"
    options = ServerOptions(
        lang=args.lang,
        max_bytes=args.max_bytes,
        inbox_path=None if args.no_inbox else resolve_inbox_path(args.inbox),
        wake_url=wake_url,
        wake_session_id=args.wake_session_id,
        wake_session_title=args.wake_session_title,
        wake_token=args.wake_token,
        wake_model=args.wake_model,
        wake_timeout=args.wake_timeout,
        wake_retries=args.wake_retries,
        wake_retry_delay=args.wake_retry_delay,
        wake_force=not args.wake_no_force,
        wake_quiet_minutes=args.wake_quiet_minutes,
        prompt_prefix=args.prompt_prefix,
        wake_words=parse_wake_words(args.wake_words),
        upload_token=args.upload_token,
        upload_rate_per_minute=args.upload_rate_per_minute,
        upload_rate_window_seconds=args.upload_rate_window_seconds,
        allowed_origins=tuple(args.allowed_origin),
    )
    validate_upload_token_requirement(args.host, options.upload_token, parser)
    server = VoiceUploadServer((args.host, args.port), VoiceUploadHandler, config=config, options=options)
    print(
        json.dumps(
            {
                "ok": True,
                "service": "stackchan_voice_upload_server",
                "url": f"http://{args.host}:{args.port}/voice/upload",
                "health": f"http://{args.host}:{args.port}/health",
                "frontend_enabled": bool(options.wake_url and options.wake_session_id),
                "inbox_enabled": options.inbox_path is not None,
                "config": build_health_payload(options)["config"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"ok": True, "event": "stop"}, ensure_ascii=False), flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
