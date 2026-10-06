"""ElevenLabs PCM synthesis, completed before any device playback begins."""

import time
from collections.abc import Iterable
from urllib.parse import quote

import requests

from .stackchan_config import PCM_SAMPLE_WIDTH, StackchanConfig


class TtsSynthesisError(RuntimeError):
    """A safe-to-display synthesis failure without provider response bodies."""


def collect_pcm(chunks: Iterable[bytes], *, max_bytes: int, deadline: float) -> bytes:
    pcm = bytearray()
    for chunk in chunks:
        if time.monotonic() > deadline:
            raise TtsSynthesisError("TTS synthesis deadline exceeded")
        if len(pcm) + len(chunk) > max_bytes:
            raise TtsSynthesisError("TTS PCM exceeds the configured playback byte limit")
        pcm.extend(chunk)
    if time.monotonic() > deadline:
        raise TtsSynthesisError("TTS synthesis deadline exceeded")
    if not pcm or len(pcm) % PCM_SAMPLE_WIDTH:
        raise TtsSynthesisError("TTS returned empty or incomplete PCM samples")
    return bytes(pcm)


def synthesize_pcm(text: str, config: StackchanConfig) -> bytes:
    if not config.elevenlabs_api_key or not config.elevenlabs_voice_id:
        raise TtsSynthesisError("ElevenLabs key or voice is not configured")
    deadline = time.monotonic() + config.elevenlabs_tts_timeout
    voice = quote(config.elevenlabs_voice_id, safe="")
    try:
        with requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
            params={"output_format": "pcm_24000"},
            headers={
                "xi-api-key": config.elevenlabs_api_key,
                "Content-Type": "application/json",
                "Accept": "application/octet-stream",
            },
            json={
                "text": text,
                "model_id": config.elevenlabs_model_id,
                "voice_settings": {
                    "stability": config.elevenlabs_stability,
                    "similarity_boost": config.elevenlabs_similarity,
                },
            },
            stream=True,
            allow_redirects=False,
            timeout=(min(10.0, config.elevenlabs_tts_timeout), config.elevenlabs_tts_timeout),
        ) as response:
            if response.status_code != 200:
                raise TtsSynthesisError(f"ElevenLabs HTTP {response.status_code}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
            if content_type not in {"audio/pcm", "audio/x-pcm", "audio/raw", "audio/x-raw", "application/octet-stream"}:
                raise TtsSynthesisError("ElevenLabs returned a non-PCM content type")
            return collect_pcm(
                response.iter_content(chunk_size=4096),
                max_bytes=config.max_pcm_payload_bytes,
                deadline=deadline,
            )
    except requests.RequestException as exc:
        # Exception URLs/bodies can echo request text or credentials.
        raise TtsSynthesisError(f"ElevenLabs transport failed ({type(exc).__name__})") from None
