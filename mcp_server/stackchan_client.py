import json
import os
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from .stackchan_config import (
    MAX_PCM_PAYLOAD_BYTES,
    PCM_CONTENT_TYPE,
    PCM_SAMPLE_WIDTH,
    PCM_SEGMENT_BYTES,
    StackchanConfig,
)


class PcmPlaybackError(RuntimeError):
    def __init__(self, message: str, *, started: bool = False):
        super().__init__(message)
        self.started = started


class StackchanClient:
    def __init__(self, config: StackchanConfig):
        self.config = config
        self.transport = os.environ.get("STACKCHAN_HTTP_TRANSPORT", "requests").lower()

    @property
    def base_url(self) -> str:
        return f"http://{self.config.stackchan_ip}:{self.config.stackchan_port}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | float = 5,
    ) -> "_HttpResult":
        url = f"{self.base_url}{path}"
        if self.transport == "curl":
            return _curl_request(
                method, url, json_body=json_body, data=data, headers=headers, timeout=timeout
            )

        try:
            response = _requests_request(
                method,
                url,
                json_body=json_body,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException:
            if self.transport == "curl-fallback":
                return _curl_request(
                    method, url, json_body=json_body, data=data, headers=headers, timeout=timeout
                )
            raise
        return response

    def play(self, wav_url: str) -> dict:
        return self._request("POST", "/play", json_body={"voice_url": wav_url}, timeout=5).json()

    def wait_for_playback_start(
        self,
        *,
        baseline_started_ms: int | None = None,
        timeout: float = 5.0,
        interval: float = 0.2,
    ) -> dict:
        deadline = time.monotonic() + timeout
        last_status: dict = {}
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                last_status = self.playback_status()
                last_error = None
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(interval)
                continue
            if last_status.get("playing"):
                return {"started": True, "status": last_status}
            started_ms = last_status.get("started_ms")
            if baseline_started_ms is not None and started_ms != baseline_started_ms:
                return {"started": True, "status": last_status}
            time.sleep(interval)
        result = {"started": False, "status": last_status}
        if last_error:
            result["error"] = last_error
        return result

    def get_audio(self) -> bytes | None:
        resp = self._request("GET", "/audio", timeout=10)
        if resp.status_code == 200:
            return resp.content
        return None

    def audio_status(self) -> dict:
        return self._request("GET", "/audio/status", timeout=3).json()

    def stream_to_host(
        self,
        *,
        port: int,
        host: str = "",
        seconds: int = 10,
        frame_samples: int = 320,
    ) -> dict:
        params = {
            "port": int(port),
            "seconds": int(seconds),
            "frame_samples": int(frame_samples),
        }
        if host:
            params["host"] = host
        return self._request("GET", f"/stream?{urlencode(params)}", timeout=seconds + 8).json()

    def playback_status(self) -> dict:
        return self._request("GET", "/playback/status", timeout=3).json()

    def move(self, x: float, y: float, speed: int) -> dict:
        return self._request(
            "POST", "/move", json_body={"x": x, "y": y, "speed": speed}, timeout=5
        ).json()

    def gesture(self, gesture: str) -> dict:
        return self._request("POST", f"/{gesture}", timeout=5).json()

    def set_face(self, face: str) -> dict:
        return self._request("POST", "/face", json_body={"face": face}, timeout=5).json()

    def snapshot(self) -> tuple[bytes | None, int]:
        with suppress(Exception):
            self._request("GET", "/snapshot", timeout=5)
        resp = self._request("GET", "/snapshot", timeout=10)
        if resp.status_code == 200:
            return resp.content, len(resp.content)
        return None, 0


@dataclass
class _HttpResult:
    status_code: int
    content: bytes
    text: str

    def json(self) -> dict:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


def _curl_request(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | float = 5,
) -> _HttpResult:
    payload = data
    request_headers = dict(headers or {})
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    with tempfile.NamedTemporaryFile() as body_file:
        cmd = [
            "/usr/bin/curl",
            "-sS",
            "--max-time",
            str(timeout),
            "-X",
            method.upper(),
            "-o",
            body_file.name,
            "-w",
            "%{http_code}",
        ]
        for key, value in request_headers.items():
            cmd.extend(["-H", f"{key}: {value}"])
        if payload is not None:
            cmd.extend(["--data-binary", "@-"])
        cmd.append(url)

        completed = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise requests.RequestException(
                f"curl request failed ({completed.returncode}): {stderr}"
            )

        body_file.seek(0)
        content = body_file.read()
    status_text = completed.stdout.decode("ascii", errors="ignore").strip() or "0"
    status_code = int(status_text[-3:]) if status_text[-3:].isdigit() else 0
    text = content.decode("utf-8", errors="replace")
    return _HttpResult(status_code, content, text)


def _requests_request(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | float = 5,
) -> requests.Response:
    method = method.upper()
    if method == "GET":
        return requests.get(url, timeout=timeout)
    if method == "POST":
        kwargs = {"timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if headers is not None:
            kwargs["headers"] = headers
        return requests.post(url, **kwargs)
    kwargs = {"timeout": timeout}
    if json_body is not None:
        kwargs["json"] = json_body
    if data is not None:
        kwargs["data"] = data
    if headers is not None:
        kwargs["headers"] = headers
    return requests.request(method, url, **kwargs)


def post_pcm_stream(client: StackchanClient, pcm_chunks, audio_dir, audio_processing) -> dict:
    import struct
    import uuid

    buffer = bytearray()
    total_size = 0
    last_result = None
    session_id = uuid.uuid4().hex
    segment_index = 0
    started = False
    pending_segment = None
    limited_samples = 0
    declicked_samples = 0
    last_segment_tail_sample = None
    saved_pcm_path = audio_dir / f"diag_{session_id}.pcm" if client.config.save_pcm else None
    saved_pcm_file = saved_pcm_path.open("wb") if saved_pcm_path is not None else None

    def post_segment(segment: bytes, *, final: bool) -> dict:
        nonlocal declicked_samples, last_segment_tail_sample, segment_index, started
        if not segment or len(segment) % PCM_SAMPLE_WIDTH != 0:
            raise ValueError(f"invalid PCM payload size: {len(segment)}")
        segment, declicked = audio_processing.declick_pcm_segment(
            segment,
            last_segment_tail_sample,
            client.config.pcm_declick_samples,
        )
        declicked_samples += declicked
        last_segment_tail_sample = struct.unpack_from(
            "<h", segment, len(segment) - PCM_SAMPLE_WIDTH
        )[0]
        try:
            resp = client._request(
                "POST",
                f"/play/pcm?session={session_id}&seq={segment_index}&final={1 if final else 0}",
                data=segment,
                headers={"Content-Type": PCM_CONTENT_TYPE},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.HTTPError as exc:
            body = getattr(exc.response, "text", "") if exc.response is not None else ""
            raise PcmPlaybackError(
                f"PCM segment HTTP failed: {exc} body={body[:200]}",
                started=started,
            ) from exc
        except ValueError as exc:
            raise PcmPlaybackError(
                f"PCM segment returned invalid JSON: {exc}", started=started
            ) from exc
        except requests.RequestException as exc:
            raise PcmPlaybackError(f"PCM segment request failed: {exc}", started=started) from exc

        if not result.get("success"):
            raise PcmPlaybackError(f"PCM segment play failed: {result}", started=started)
        started = True
        segment_index += 1
        return result

    try:
        for chunk in pcm_chunks:
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > MAX_PCM_PAYLOAD_BYTES:
                message = f"PCM payload too large: {total_size} bytes exceeds {MAX_PCM_PAYLOAD_BYTES} byte limit"
                if started:
                    raise PcmPlaybackError(message, started=True)
                raise ValueError(message)
            if saved_pcm_file is not None:
                saved_pcm_file.write(chunk)
            conditioned_chunk, limited = audio_processing.condition_pcm_chunk(
                chunk,
                gain=client.config.pcm_gain,
                limit=client.config.pcm_limit,
            )
            limited_samples += limited
            buffer.extend(conditioned_chunk)
            while len(buffer) >= PCM_SEGMENT_BYTES:
                segment_size = PCM_SEGMENT_BYTES - (PCM_SEGMENT_BYTES % PCM_SAMPLE_WIDTH)
                segment_size = audio_processing.choose_pcm_segment_cut(
                    buffer,
                    segment_size,
                    client.config.pcm_zero_cross_window,
                )
                if pending_segment is not None:
                    last_result = post_segment(pending_segment, final=False)
                pending_segment = bytes(buffer[:segment_size])
                del buffer[:segment_size]

        if not buffer and pending_segment is None and last_result is None:
            raise ValueError("invalid PCM payload size: 0")
        if len(buffer) % PCM_SAMPLE_WIDTH != 0:
            message = f"invalid PCM payload size: {len(buffer)}"
            if started:
                raise PcmPlaybackError(message, started=True)
            raise ValueError(message)
        if buffer:
            if pending_segment is not None:
                last_result = post_segment(pending_segment, final=False)
            last_result = post_segment(bytes(buffer), final=True)
        elif pending_segment is not None:
            last_result = post_segment(pending_segment, final=True)
    finally:
        if saved_pcm_file is not None:
            saved_pcm_file.close()

    result = last_result or {"success": False, "error": "no pcm"}
    result.setdefault("session", session_id)
    result.setdefault("segments", segment_index)
    result.setdefault("total_bytes", total_size)
    result.setdefault("pcm_gain", client.config.pcm_gain)
    result.setdefault("pcm_limit", client.config.pcm_limit)
    result.setdefault("limited_samples", limited_samples)
    result.setdefault("declick_samples", client.config.pcm_declick_samples)
    result.setdefault("declicked_samples", declicked_samples)
    if saved_pcm_path is not None:
        result.setdefault("saved_pcm", str(saved_pcm_path))
    return result
