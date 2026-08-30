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
  - Returns `{"ready":true|false,"mode":"mcp","source":"voice|touch|none"}`.
  - `source=touch` marks a recording explicitly started by tapping the top
    touch strip. The host bridge uses this marker to bypass wake-word matching
    and forwards the transcript with the `（触摸）` prefix.

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

## Touch Strip

- The three-zone Si12T strip on top of Stack-chan is polled by the firmware.
- A short tap starts a forced microphone recording without waiting for the
  normal RMS trigger. The user has up to four seconds to begin speaking; after
  speech starts, the normal silence and maximum-duration rules end recording.
  Audio playback, PCM streaming, or another recording causes the request to be
  rejected instead of interrupting the active path.
- A forward swipe followed by a backward swipe, or the reverse order, within
  1500 ms is treated as petting. While audio and recording are idle, petting
  shows the `happy` face for five seconds and starts the non-blocking shake
  gesture. Touch never starts speech or a network request.
- The CoreS3 camera shares GPIO 11/12 with the internal I2C bus. `/snapshot`
  temporarily suspends touch, uses the camera, then restores the I2C bus and
  reinitializes the Si12T on every success and failure path.

## Diagnostics

- `GET /env`
  - Returns temperature, humidity, and barometric pressure when available.
  - Success shape:
    `{"success":true,"temperature":24.3,"humidity":58.2,"pressure":1013.2,"sensors":{"sht31":true,"qmp6988":true}}`
  - Missing sensors or unavailable readings are returned as `null`.
  - If no ENV sensor is detected, returns:
    `{"success":false,"error":"no env sensor detected"}`.

- `GET /env/debug`
  - Returns raw environmental sensor diagnostics used to validate QMP6988
    calibration and ADC readings on the host side.

- `GET /servo/status`
- `GET /touch/status`
  - Returns whether the Si12T is available or temporarily suspended, the
    current front/middle/back intensity values, the latest click/hold/swipe or
    petting event, recording request/failure counters, pet counters, and
    `resume_failure_count` for camera handoff failures.
- `GET /playback/status`
  - Includes playback state, PCM queue depth, audio queue depth, download
    queue depth, UDP/TCP PCM stream state, and whether a WAV download is
    currently in flight.
  - `download_age_ms` reports the age of the active WAV download, or `0` when
    idle. `download_watchdog_ms` reports the automatic recovery threshold.
- `GET /snapshot`
  - Returns a JPEG image.
