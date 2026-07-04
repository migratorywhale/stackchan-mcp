import socket
import struct
import time
from contextlib import suppress

import requests

from .stackchan_config import (
    PCM_CONTENT_TYPE,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    PCM_UDP_FRAME_BYTES,
    PCM_UDP_FRAME_MS,
    StackchanConfig,
)


class PcmPlaybackError(RuntimeError):
    def __init__(self, message: str, *, started: bool = False):
        super().__init__(message)
        self.started = started


class StackchanClient:
    def __init__(self, config: StackchanConfig):
        self.config = config

    @property
    def base_url(self) -> str:
        return f"http://{self.config.stackchan_ip}:{self.config.stackchan_port}"

    def play(self, wav_url: str) -> dict:
        return requests.post(
            f"{self.base_url}/play",
            json={"voice_url": wav_url},
            timeout=self.config.http_play_timeout,
        ).json()

    def wait_for_playback_start(
        self,
        *,
        baseline_started_ms: int | None = None,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> dict:
        timeout = self.config.playback_start_timeout if timeout is None else timeout
        interval = self.config.playback_poll_interval if interval is None else interval
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
        resp = requests.get(f"{self.base_url}/audio", timeout=self.config.http_audio_timeout)
        if resp.status_code == 200:
            return resp.content
        return None

    def audio_status(self) -> dict:
        return requests.get(f"{self.base_url}/audio/status", timeout=self.config.http_status_timeout).json()

    def playback_status(self) -> dict:
        return requests.get(f"{self.base_url}/playback/status", timeout=self.config.http_status_timeout).json()

    def start_audio_session(self) -> dict:
        return requests.post(
            f"{self.base_url}/audio/session",
            json={
                "codec": "pcm_s16le",
                "sample_rate": PCM_SAMPLE_RATE,
                "channels": 1,
                "sample_width": PCM_SAMPLE_WIDTH,
                "frame_ms": PCM_UDP_FRAME_MS,
            },
            timeout=self.config.http_command_timeout,
        ).json()

    def stop_audio_session(self, session_id: str) -> dict:
        return requests.delete(
            f"{self.base_url}/audio/session/{session_id}",
            timeout=self.config.http_command_timeout,
        ).json()

    def move(self, x: float, y: float, speed: int) -> dict:
        return requests.post(
            f"{self.base_url}/move",
            json={"x": x, "y": y, "speed": speed},
            timeout=self.config.http_command_timeout,
        ).json()

    def gesture(self, gesture: str) -> dict:
        return requests.post(f"{self.base_url}/{gesture}", timeout=self.config.http_command_timeout).json()

    def set_face(self, face: str) -> dict:
        return requests.post(
            f"{self.base_url}/face",
            json={"face": face},
            timeout=self.config.http_command_timeout,
        ).json()

    def snapshot(self) -> tuple[bytes | None, int]:
        with suppress(Exception):
            requests.get(f"{self.base_url}/snapshot", timeout=self.config.http_snapshot_warmup_timeout)
        resp = requests.get(f"{self.base_url}/snapshot", timeout=self.config.http_snapshot_timeout)
        if resp.status_code == 200:
            return resp.content, len(resp.content)
        return None, 0


def post_pcm_stream(client: StackchanClient, pcm_chunks, audio_dir, audio_processing) -> dict:
    import struct
    import uuid

    started_at = time.perf_counter()
    buffer = bytearray()
    total_size = 0
    last_result = None
    session_id = uuid.uuid4().hex
    segment_index = 0
    started = False
    first_chunk_ms = None
    first_segment_ms = None
    pending_segment = None
    pending_sample_bytes = b""
    limited_samples = 0
    declicked_samples = 0
    last_segment_tail_sample = None
    saved_pcm_path = audio_dir / f"diag_{session_id}.pcm" if client.config.save_pcm else None
    saved_pcm_file = saved_pcm_path.open("wb") if saved_pcm_path is not None else None

    def post_segment(segment: bytes, *, final: bool) -> dict:
        nonlocal declicked_samples, first_segment_ms, last_segment_tail_sample, segment_index, started
        if not segment or len(segment) % PCM_SAMPLE_WIDTH != 0:
            raise ValueError(f"invalid PCM payload size: {len(segment)}")
        segment, declicked = audio_processing.declick_pcm_segment(
            segment,
            last_segment_tail_sample,
            client.config.pcm_declick_samples,
        )
        declicked_samples += declicked
        last_segment_tail_sample = struct.unpack_from("<h", segment, len(segment) - PCM_SAMPLE_WIDTH)[0]
        url = (
            f"{client.base_url}/play/pcm?session={session_id}&seq={segment_index}"
            f"&final={1 if final else 0}&mode=staged"
        )
        try:
            resp = requests.post(
                url,
                data=segment,
                headers={
                    "Content-Type": PCM_CONTENT_TYPE,
                    "X-Stackchan-Pcm-Session": session_id,
                    "X-Stackchan-Pcm-Seq": str(segment_index),
                    "X-Stackchan-Pcm-Final": "1" if final else "0",
                    "X-Stackchan-Pcm-Mode": "staged",
                },
                timeout=client.config.pcm_segment_post_timeout,
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
            raise PcmPlaybackError(f"PCM segment returned invalid JSON: {exc}", started=started) from exc
        except requests.RequestException as exc:
            raise PcmPlaybackError(f"PCM segment request failed: {exc}", started=started) from exc

        if not result.get("success"):
            raise PcmPlaybackError(f"PCM segment play failed: {result}", started=started)
        if not result.get("staged"):
            started = True
        if first_segment_ms is None:
            first_segment_ms = round((time.perf_counter() - started_at) * 1000)
        segment_index += 1
        return result

    try:
        for chunk in pcm_chunks:
            if not chunk:
                continue
            if first_chunk_ms is None:
                first_chunk_ms = round((time.perf_counter() - started_at) * 1000)
            total_size += len(chunk)
            if total_size > client.config.max_pcm_payload_bytes:
                message = (
                    f"PCM payload too large: {total_size} bytes exceeds "
                    f"{client.config.max_pcm_payload_bytes} byte limit"
                )
                if started:
                    raise PcmPlaybackError(message, started=True)
                raise ValueError(message)
            if saved_pcm_file is not None:
                saved_pcm_file.write(chunk)
            if pending_sample_bytes:
                chunk = pending_sample_bytes + chunk
                pending_sample_bytes = b""
            if len(chunk) % PCM_SAMPLE_WIDTH != 0:
                pending_sample_bytes = chunk[-(len(chunk) % PCM_SAMPLE_WIDTH) :]
                chunk = chunk[: -(len(chunk) % PCM_SAMPLE_WIDTH)]
            if not chunk:
                continue
            elapsed = time.perf_counter() - started_at
            if (
                not started
                and client.config.pcm_first_segment_timeout > 0
                and elapsed > client.config.pcm_first_segment_timeout
            ):
                raise ValueError(
                    "PCM first segment timeout: "
                    f"{len(buffer) + len(chunk)} bytes buffered in {elapsed:.1f}s"
                )
            conditioned_chunk, limited = audio_processing.condition_pcm_chunk(
                chunk,
                gain=client.config.pcm_gain,
                limit=client.config.pcm_limit,
            )
            limited_samples += limited
            buffer.extend(conditioned_chunk)
            while len(buffer) >= client.config.pcm_segment_bytes:
                segment_size = client.config.pcm_segment_bytes - (
                    client.config.pcm_segment_bytes % PCM_SAMPLE_WIDTH
                )
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
        if pending_sample_bytes:
            message = f"invalid PCM payload size: trailing {len(pending_sample_bytes)} byte partial sample"
            if started:
                raise PcmPlaybackError(message, started=True)
            raise ValueError(message)
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
    result.setdefault(
        "timing_ms",
        {
            "pcm_total": round((time.perf_counter() - started_at) * 1000),
            "fish_first_chunk": first_chunk_ms,
            "first_segment_posted": first_segment_ms,
        },
    )
    if saved_pcm_path is not None:
        result.setdefault("saved_pcm", str(saved_pcm_path))
    return result


def post_pcm_tcp_stream(client: StackchanClient, pcm_chunks, audio_dir, audio_processing) -> dict:
    import uuid

    started_at = time.perf_counter()
    session_id = uuid.uuid4().hex
    first_chunk_ms = None
    first_send_ms = None
    total_input_size = 0
    total_sent_size = 0
    limited_samples = 0
    pending_sample_bytes = b""
    saved_pcm_path = audio_dir / f"diag_{session_id}.pcm" if client.config.save_pcm else None
    saved_pcm_file = saved_pcm_path.open("wb") if saved_pcm_path is not None else None
    initial_buffer = bytearray()
    chunks_iter = iter(pcm_chunks)
    accepted = False
    exhausted = False

    def condition_chunk(chunk: bytes) -> bytes:
        nonlocal first_chunk_ms, limited_samples, pending_sample_bytes, total_input_size
        if not chunk:
            return b""
        if first_chunk_ms is None:
            first_chunk_ms = round((time.perf_counter() - started_at) * 1000)
        total_input_size += len(chunk)
        if total_input_size > client.config.max_pcm_payload_bytes:
            raise ValueError(
                f"PCM payload too large: {total_input_size} bytes exceeds "
                f"{client.config.max_pcm_payload_bytes} byte limit"
            )
        if saved_pcm_file is not None:
            saved_pcm_file.write(chunk)
        if pending_sample_bytes:
            chunk = pending_sample_bytes + chunk
            pending_sample_bytes = b""
        if len(chunk) % PCM_SAMPLE_WIDTH != 0:
            pending_sample_bytes = chunk[-(len(chunk) % PCM_SAMPLE_WIDTH) :]
            chunk = chunk[: -(len(chunk) % PCM_SAMPLE_WIDTH)]
        if not chunk:
            return b""
        conditioned_chunk, limited = audio_processing.condition_pcm_chunk(
            chunk,
            gain=client.config.pcm_gain,
            limit=client.config.pcm_limit,
        )
        limited_samples += limited
        return conditioned_chunk

    def next_conditioned_chunk() -> bytes | None:
        nonlocal exhausted
        for chunk in chunks_iter:
            conditioned = condition_chunk(chunk)
            if conditioned:
                return conditioned
        exhausted = True
        return None

    try:
        while len(initial_buffer) < client.config.pcm_stream_initial_buffer_bytes:
            chunk = next_conditioned_chunk()
            if chunk is None:
                break
            initial_buffer.extend(chunk)
            elapsed = time.perf_counter() - started_at
            if client.config.pcm_first_segment_timeout > 0 and elapsed > client.config.pcm_first_segment_timeout:
                raise ValueError(
                    "PCM stream initial buffer timeout: "
                    f"{len(initial_buffer)} bytes buffered in {elapsed:.1f}s"
                )
        if exhausted and pending_sample_bytes:
            raise ValueError(f"invalid PCM payload size: trailing {len(pending_sample_bytes)} byte partial sample")
        if not initial_buffer:
            raise ValueError("invalid PCM payload size: 0")

        address = (client.config.stackchan_ip, client.config.pcm_stream_port)
        header = (
            f"STACKCHAN_PCM_STREAM/1 session={session_id} rate={PCM_SAMPLE_RATE} "
            "channels=1 width=2\n"
        ).encode("ascii")
        try:
            with socket.create_connection(address, timeout=client.config.pcm_stream_connect_timeout) as sock:
                sock.settimeout(client.config.pcm_stream_io_timeout)
                sock.sendall(header)
                response = bytearray()
                while not response.endswith(b"\n") and len(response) < 80:
                    chunk = sock.recv(1)
                    if not chunk:
                        break
                    response.extend(chunk)
                if response != b"OK\n":
                    message = response.decode("ascii", errors="replace").strip() or "no response"
                    raise PcmPlaybackError(f"PCM TCP stream rejected: {message}", started=False)
                accepted = True

                sock.sendall(initial_buffer)
                total_sent_size += len(initial_buffer)
                first_send_ms = round((time.perf_counter() - started_at) * 1000)

                while True:
                    chunk = next_conditioned_chunk()
                    if chunk is None:
                        break
                    sock.sendall(chunk)
                    total_sent_size += len(chunk)
                if pending_sample_bytes:
                    raise PcmPlaybackError(
                        f"invalid PCM payload size: trailing {len(pending_sample_bytes)} byte partial sample",
                        started=True,
                    )
        except PcmPlaybackError:
            raise
        except OSError as exc:
            raise PcmPlaybackError(f"PCM TCP stream failed: {exc}", started=accepted) from exc
    finally:
        if saved_pcm_file is not None:
            saved_pcm_file.close()

    result = {
        "success": True,
        "session": session_id,
        "transport": "tcp",
        "total_bytes": total_input_size,
        "sent_bytes": total_sent_size,
        "pcm_gain": client.config.pcm_gain,
        "pcm_limit": client.config.pcm_limit,
        "limited_samples": limited_samples,
        "timing_ms": {
            "pcm_total": round((time.perf_counter() - started_at) * 1000),
            "fish_first_chunk": first_chunk_ms,
            "first_stream_send": first_send_ms,
        },
    }
    if saved_pcm_path is not None:
        result["saved_pcm"] = str(saved_pcm_path)
    return result


def status_int(status: dict, *keys: str) -> int:
    for key in keys:
        value = status.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def wait_for_udp_completion_status(client: StackchanClient, session_id: str) -> dict:
    # Firmware's preroll-failure timer is PCM_UDP_PREROLL_TIMEOUT_MS (5s); the poll window
    # must outlive it or we give up before the device reports a real completion status.
    timeout = max(client.config.playback_start_timeout, 6.0)
    interval = max(client.config.playback_poll_interval, 0.05)
    deadline = time.monotonic() + timeout
    last_status: dict = {}
    while time.monotonic() < deadline:
        try:
            last_status = client.playback_status()
        except requests.RequestException:
            break
        current_session = str(last_status.get("udp_audio_session") or "")
        udp_active = bool(last_status.get("udp_audio_active") or last_status.get("udp_audio_playing"))
        if not udp_active and current_session in {"", session_id}:
            return last_status
        time.sleep(interval)
    return last_status


def _fade_pcm_tail_to_silence(buffer: bytearray, real_len: int, samples: int) -> int:
    """Ramp the last `samples` samples of the real (pre-padding) audio in `buffer` toward
    silence, mirroring the weighting used by audio_processing.declick_pcm_segment but applied
    at the tail instead of the head (that helper only smooths a segment's leading edge)."""
    if samples <= 0 or real_len <= 0:
        return 0
    sample_count = real_len // PCM_SAMPLE_WIDTH
    ramp = min(samples, sample_count)
    if ramp <= 0:
        return 0
    start_sample = sample_count - ramp
    for offset in range(ramp):
        pos = (start_sample + offset) * PCM_SAMPLE_WIDTH
        current = struct.unpack_from("<h", buffer, pos)[0]
        weight = (ramp - offset) / (ramp + 1)
        struct.pack_into("<h", buffer, pos, round(current * weight))
    return ramp


def post_pcm_udp_stream(client: StackchanClient, pcm_chunks, audio_dir, audio_processing) -> dict:
    import uuid

    started_at = time.perf_counter()
    diagnostic_id = uuid.uuid4().hex
    first_chunk_ms = None
    first_send_ms = None
    total_input_size = 0
    total_sent_size = 0
    limited_samples = 0
    declicked_samples = 0
    pending_sample_bytes = b""
    frame_buffer = bytearray()
    seq = 0
    session_id = ""
    accepted = False
    saved_pcm_path = audio_dir / f"diag_{diagnostic_id}.pcm" if client.config.save_pcm else None
    saved_pcm_file = saved_pcm_path.open("wb") if saved_pcm_path is not None else None

    session = client.start_audio_session()
    if not session.get("success"):
        raise PcmPlaybackError(f"PCM UDP session failed: {session}", started=False)
    accepted = True
    session_id = str(session.get("session", ""))
    token = int(session.get("token", 0))
    udp_port = int(session.get("udp_port", 0))
    if not session_id or token == 0 or udp_port <= 0:
        raise PcmPlaybackError(f"PCM UDP session returned invalid metadata: {session}", started=False)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(client.config.pcm_stream_io_timeout)
    address = (client.config.stackchan_ip, udp_port)
    frame_seconds = (PCM_UDP_FRAME_MS / 1000.0) * client.config.udp_pace_factor
    burst_frames = max(1, int(40 / PCM_UDP_FRAME_MS))
    # Firmware's UDP ring holds 512 frames; never schedule further ahead of wall clock than
    # this, or a post-stall catch-up burst could overwrite slots the device hasn't played yet.
    max_lead_seconds = 256 * frame_seconds
    pacing_started_at: float | None = None
    next_send_at: float | None = None

    def send_frame(frame: bytes, *, end: bool = False) -> None:
        nonlocal first_send_ms, pacing_started_at, next_send_at, seq, total_sent_size
        now = time.perf_counter()
        if not end and pacing_started_at is not None and seq >= burst_frames and next_send_at is not None:
            delay = next_send_at - now
            if delay > 0:
                time.sleep(delay)
                now = time.perf_counter()
        flags = 1 if end else 0
        payload = b"" if end else frame
        header = struct.pack(
            "<4sBBHIIIHH",
            b"SCP1",
            1,
            flags,
            24,
            token,
            seq,
            seq * (PCM_SAMPLE_RATE * PCM_UDP_FRAME_MS // 1000),
            len(payload),
            0,
        )
        packet = header + payload
        sock.sendto(packet, address)
        if not end:
            total_sent_size += len(payload)
            if first_send_ms is None:
                first_send_ms = round((time.perf_counter() - started_at) * 1000)
                pacing_started_at = time.perf_counter()
                next_send_at = pacing_started_at
            seq += 1
            if pacing_started_at is not None and seq >= burst_frames:
                # Cumulative schedule: never rebase to "now". Rebasing after a stall (the old
                # `max(now, ...)` behavior) silently discards the lost time and drains the
                # device's buffer instead of sending a catch-up burst to refill it.
                base = next_send_at if next_send_at is not None else now
                next_send_at = base + frame_seconds
                lead = next_send_at - time.perf_counter()
                if lead > max_lead_seconds:
                    time.sleep(lead - max_lead_seconds)

    def condition_chunk(chunk: bytes) -> bytes:
        nonlocal first_chunk_ms, limited_samples, pending_sample_bytes, total_input_size
        if not chunk:
            return b""
        if first_chunk_ms is None:
            first_chunk_ms = round((time.perf_counter() - started_at) * 1000)
        total_input_size += len(chunk)
        if total_input_size > client.config.max_pcm_payload_bytes:
            raise PcmPlaybackError(
                f"PCM payload too large: {total_input_size} bytes exceeds "
                f"{client.config.max_pcm_payload_bytes} byte limit",
                started=accepted,
            )
        if saved_pcm_file is not None:
            saved_pcm_file.write(chunk)
        if pending_sample_bytes:
            chunk = pending_sample_bytes + chunk
            pending_sample_bytes = b""
        if len(chunk) % PCM_SAMPLE_WIDTH != 0:
            pending_sample_bytes = chunk[-(len(chunk) % PCM_SAMPLE_WIDTH) :]
            chunk = chunk[: -(len(chunk) % PCM_SAMPLE_WIDTH)]
        if not chunk:
            return b""
        conditioned_chunk, limited = audio_processing.condition_pcm_chunk(
            chunk,
            gain=client.config.pcm_gain,
            limit=client.config.pcm_limit,
        )
        limited_samples += limited
        return conditioned_chunk

    try:
        for chunk in pcm_chunks:
            conditioned = condition_chunk(chunk)
            if not conditioned:
                continue
            frame_buffer.extend(conditioned)
            elapsed = time.perf_counter() - started_at
            if first_send_ms is None and client.config.pcm_first_segment_timeout > 0 and elapsed > client.config.pcm_first_segment_timeout:
                raise ValueError(
                    "PCM UDP first frame timeout: "
                    f"{len(frame_buffer)} bytes buffered in {elapsed:.1f}s"
                )
            while len(frame_buffer) >= PCM_UDP_FRAME_BYTES:
                frame = bytes(frame_buffer[:PCM_UDP_FRAME_BYTES])
                del frame_buffer[:PCM_UDP_FRAME_BYTES]
                if seq == 0:
                    frame, faded_in = audio_processing.declick_pcm_segment(
                        frame, 0, client.config.pcm_declick_samples
                    )
                    declicked_samples += faded_in
                send_frame(frame)
        if pending_sample_bytes:
            raise PcmPlaybackError(
                f"invalid PCM payload size: trailing {len(pending_sample_bytes)} byte partial sample",
                started=accepted,
            )
        if frame_buffer:
            real_len = len(frame_buffer)
            frame_buffer.extend(b"\x00" * (PCM_UDP_FRAME_BYTES - real_len))
            if seq == 0:
                padded, faded_in = audio_processing.declick_pcm_segment(
                    bytes(frame_buffer), 0, client.config.pcm_declick_samples
                )
                declicked_samples += faded_in
                frame_buffer[:] = padded
            declicked_samples += _fade_pcm_tail_to_silence(
                frame_buffer, real_len, client.config.pcm_declick_samples
            )
            send_frame(bytes(frame_buffer))
        if seq == 0:
            raise ValueError("invalid PCM payload size: 0")
        for _ in range(3):
            send_frame(b"", end=True)
            time.sleep(0.005)
    except OSError as exc:
        raise PcmPlaybackError(f"PCM UDP stream failed: {exc}", started=accepted) from exc
    finally:
        sock.close()
        if saved_pcm_file is not None:
            saved_pcm_file.close()

    completion_status = wait_for_udp_completion_status(client, session_id)
    frames_received = status_int(completion_status, "udp_last_frames_received", "frames_received")
    frames_lost = status_int(completion_status, "udp_last_frames_lost", "frames_lost")
    underruns = status_int(completion_status, "udp_last_underruns", "underruns")
    end_reason = str(completion_status.get("udp_last_end_reason") or "")

    result = {
        "success": True,
        "session": session_id,
        "transport": "udp",
        "frames": seq,
        "segments": seq,
        "total_bytes": total_input_size,
        "sent_bytes": total_sent_size,
        "pcm_gain": client.config.pcm_gain,
        "pcm_limit": client.config.pcm_limit,
        "limited_samples": limited_samples,
        "declicked_samples": declicked_samples,
        "timing_ms": {
            "pcm_total": round((time.perf_counter() - started_at) * 1000),
            "fish_first_chunk": first_chunk_ms,
            "first_udp_frame": first_send_ms,
        },
        "udp_pace_factor": client.config.udp_pace_factor,
        "udp_frames_received": frames_received,
        "udp_frames_lost": frames_lost,
        "udp_underruns": underruns,
        "udp_end_reason": end_reason,
    }
    if saved_pcm_path is not None:
        result["saved_pcm"] = str(saved_pcm_path)
    # A totally missing status (device unreachable) is distinct from a status that reports
    # zero frames received, so check it first and fail loudly rather than assume success.
    if not completion_status:
        raise PcmPlaybackError(
            "PCM UDP playback could not be verified: no completion status from device "
            f"(sent_frames={seq}, session={session_id})",
            started=True,
        )
    # This dominates whatever else the status reports: a completion status -- even a stale
    # one left over from a previous session -- can never mask the device having received
    # nothing at all for this session.
    if seq > 0 and frames_received == 0:
        raise PcmPlaybackError(
            "PCM UDP playback incomplete: device received no frames "
            f"(sent_frames={seq}, session={session_id})",
            started=True,
        )
    if (
        frames_lost > 0
        or underruns > 0
        or (frames_received > 0 and frames_received < seq)
        or (end_reason and end_reason != "end")
    ):
        raise PcmPlaybackError(
            "PCM UDP playback incomplete: "
            f"sent_frames={seq} received={frames_received} "
            f"lost={frames_lost} underruns={underruns} reason={end_reason or '?'}",
            started=True,
        )
    return result
