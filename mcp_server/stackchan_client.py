from contextlib import suppress

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

    @property
    def base_url(self) -> str:
        return f"http://{self.config.stackchan_ip}:{self.config.stackchan_port}"

    def play(self, wav_url: str) -> dict:
        return requests.post(f"{self.base_url}/play", json={"voice_url": wav_url}, timeout=5).json()

    def get_audio(self) -> bytes | None:
        resp = requests.get(f"{self.base_url}/audio", timeout=10)
        if resp.status_code == 200:
            return resp.content
        return None

    def audio_status(self) -> dict:
        return requests.get(f"{self.base_url}/audio/status", timeout=3).json()

    def playback_status(self) -> dict:
        return requests.get(f"{self.base_url}/playback/status", timeout=3).json()

    def move(self, x: float, y: float, speed: int) -> dict:
        return requests.post(
            f"{self.base_url}/move",
            json={"x": x, "y": y, "speed": speed},
            timeout=5,
        ).json()

    def gesture(self, gesture: str) -> dict:
        return requests.post(f"{self.base_url}/{gesture}", timeout=5).json()

    def set_face(self, face: str) -> dict:
        return requests.post(f"{self.base_url}/face", json={"face": face}, timeout=5).json()

    def snapshot(self) -> tuple[bytes | None, int]:
        with suppress(Exception):
            requests.get(f"{self.base_url}/snapshot", timeout=5)
        resp = requests.get(f"{self.base_url}/snapshot", timeout=10)
        if resp.status_code == 200:
            return resp.content, len(resp.content)
        return None, 0


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
        last_segment_tail_sample = struct.unpack_from("<h", segment, len(segment) - PCM_SAMPLE_WIDTH)[0]
        url = f"{client.base_url}/play/pcm?session={session_id}&seq={segment_index}&final={1 if final else 0}"
        try:
            resp = requests.post(
                url,
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
            raise PcmPlaybackError(f"PCM segment returned invalid JSON: {exc}", started=started) from exc
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
