# Stack-chan HTTP API Contract

This is the shared contract between the CoreS3 firmware and the MCP server.

## Audio Playback

- `POST /play`
  - JSON body: `{"voice_url":"http://.../file.wav"}`
  - Queues a WAV URL for device-side download and playback.

- `POST /play/pcm?session=<id>&seq=<n>&final=<0|1>`
  - Body: raw PCM bytes.
  - Format: `24 kHz`, mono, signed 16-bit little-endian PCM.
  - Content type: `audio/x-raw;format=s16le;rate=24000;channels=1`.
  - Firmware accepts one active PCM session at a time. Segments must arrive in
    increasing `seq` order.
  - Firmware request body limit: `128 KiB`.
  - MCP total PCM payload limit: `2 MiB`.

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

- `GET /stream?port=<host-port>[&host=<host-ip>][&seconds=10][&frame_samples=320]`
  - Experimental, lower-latency microphone path.
  - The firmware connects to the host TCP listener and pushes raw PCM.
  - Format: `16 kHz`, mono, signed 16-bit little-endian PCM.
  - If `host` is omitted, firmware connects back to the HTTP requester's IP.
  - The endpoint refuses while playback/speaker is active.
  - This is not the stable default recording path; keep clients on
    `/audio/status` + `/audio` unless they explicitly opt into streaming.

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
- `GET /snapshot`
  - Returns a JPEG image.
