# Stack-chan HTTP API Contract

This is the shared contract between the CoreS3 firmware and the MCP server.

## Audio Playback

- `POST /play`
  - JSON body: `{"voice_url":"http://.../file.wav"}`
  - Queues a WAV URL for device-side download and playback.

- `POST /audio/session`
  - JSON body: `{"codec":"pcm_s16le","sample_rate":24000,"channels":1,"sample_width":2,"frame_ms":10}`.
  - Starts a low-latency UDP PCM session.
  - Returns `session`, `token`, and `udp_port`.
  - Audio datagrams use magic `SCP1`, version `1`, the returned token, a
    sequence number, sample timestamp, payload length, and one 10 ms PCM frame.
  - An end packet uses the same header with `flags=1` and no payload.

- `DELETE /audio/session/<session>`
  - Stops the active UDP PCM session.

- `POST /play/pcm?session=<id>&seq=<n>&final=<0|1>`
  - Body: raw PCM bytes.
  - Format: `24 kHz`, mono, signed 16-bit little-endian PCM.
  - Content type: `audio/x-raw;format=s16le;rate=24000;channels=1`.
  - `mode=staged` or `X-Stackchan-Pcm-Mode: staged` buffers all segments in
    PSRAM and starts playback only after the final segment.
  - PCM metadata can also be sent with headers:
    `X-Stackchan-Pcm-Session`, `X-Stackchan-Pcm-Seq`, and
    `X-Stackchan-Pcm-Final`. Headers are preferred for raw uploads; query
    parameters remain supported for compatibility.
  - Firmware accepts one active PCM session at a time. Segments must arrive in
    increasing `seq` order.
  - Firmware request body limit: `128 KiB`.
  - MCP total PCM payload limit: `2 MiB`.

- TCP PCM stream on port `9090`
  - Client sends one ASCII header line:
    `STACKCHAN_PCM_STREAM/1 session=<id> rate=24000 channels=1 width=2\n`.
  - Firmware replies `OK\n` or `ERR <code>\n`.
  - After `OK\n`, the client sends raw `24 kHz`, mono, signed 16-bit
    little-endian PCM bytes until TCP EOF.
  - This is the default live PCM transport. Firmware prebuffers about 120 ms,
    writes 10 ms frames to the CoreS3 speaker through ESP-IDF I2S DMA, and
    exposes stream diagnostics through `/playback/status`.

## Recording

- `POST /mode`
  - JSON body: `{"mode":"mcp"}`.
  - Clears any previous recording. Recording behavior is always MCP pull mode.

- `GET /audio/status`
  - Returns `{"ready":true|false,"mode":"mcp"}`.

- `GET /audio`
  - Returns the latest WAV recording.
  - This is a consuming read: after a successful response, the recording is no
    longer reported as ready.

## Motion

- `POST /move`
  - JSON body: `{"x": <yaw degrees>, "y": <pitch degrees>, "speed": <0-100>}`.

- `POST /home`
- `POST /nod`
- `POST /shake`

## Face

- `POST /face`
  - JSON body: `{"face":"calm"}`.
  - Valid names: `calm`, `thinking`, `happy`, `sleepy`, `shy`, `smug`, `pouty`.

- `GET /face`
  - Returns `{"face":"<name>"}`.

## Diagnostics

- `GET /servo/status`
- `GET /playback/status`
  - Includes playback state, PCM queue depth, audio queue depth, download
    queue depth, UDP/TCP PCM stream state, and whether a WAV download is
    currently in flight.
- `GET /snapshot`
  - Returns a JPEG image.
