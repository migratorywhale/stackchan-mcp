# Stack-chan Development Guide

This document is a local, English-language reference for developing the
push-based Stack-chan voice avatar in this repository.

## Repository Map

- `firmware/`: Arduino/PlatformIO firmware for M5Stack CoreS3.
- `firmware/src/`: firmware services for HTTP control, microphone capture,
  playback, face display, servos, camera, Wi-Fi, notifications, and chat.
- `firmware/data/`: SPIFFS face PNGs that are uploaded to the device.
- `firmware/config.h.example`: safe template for local Wi-Fi, audio, and display
  settings.
- `faces/`: source or companion face assets.
- `mcp-server/server.py`: Python MCP server that exposes Stack-chan tools and
  talks to the device over HTTP.
- `start-http.sh`: helper script that starts the MCP server in Streamable HTTP
  mode and launches the public Cloudflare tunnel.

Do not use `CLAUDE.md` files for this project. They belong to another assistant.
Do not overwrite `firmware/src/config.h`; it may contain local secrets.

## Firmware Overview

The firmware runs on M5Stack CoreS3 with Arduino through PlatformIO. The main
loop is intentionally small:

1. Update M5Unified state.
2. Serve local HTTP requests on port 80.
3. Reconnect Wi-Fi if needed.
4. Check pending playback downloads.
5. Update lip sync.
6. Update microphone capture.
7. Detect playback completion and resume the microphone.
8. Periodically check notification work.

Key files:

- `firmware/src/main.cpp`: device setup and main loop orchestration.
- `firmware/src/http_server.cpp`: local HTTP API exposed by the device.
- `firmware/src/mic_service.cpp`: microphone trigger, pre-trigger buffer,
  WAV building, and API/MCP recording behavior.
- `firmware/src/playback_service.cpp`: non-blocking audio download, speaker
  playback, and lip sync.
- `firmware/src/face_service.cpp`: SPIFFS PNG face loading and expression
  switching.
- `firmware/src/servo_service.cpp`: SCServo yaw/pitch control and diagnostics.
- `firmware/src/camera_service.cpp`: CoreS3 GC0308 camera capture and JPEG
  conversion.
- `firmware/src/wifi_manager.cpp`: ordered Wi-Fi connection attempts and active
  backend URL selection.

## Build And Upload

Run PlatformIO commands from `firmware/`:

```sh
cd firmware
pio run
pio run -t upload
pio device monitor
pio run -t uploadfs
```

`uploadfs` is required after changing files under `firmware/data/`, including
face PNG assets.

The current PlatformIO environment is `m5stack-cores3`:

- Platform: `espressif32`
- Board: `m5stack-cores3`
- Framework: `arduino`
- Upload speed: `1500000`
- Monitor speed: `115200`
- Filesystem: `spiffs`
- Partition table: `default_16MB.csv`

The serial device on this Mac is often `/dev/cu.usbmodem101`, but verify it
before upload because it can change.

## Local Configuration

Create local firmware configuration from the example:

```sh
cp firmware/config.h.example firmware/src/config.h
```

Then edit `firmware/src/config.h` locally. Keep secrets out of commits.

Important configuration groups:

- `WIFI_NETWORK_COUNT`, `WIFI_SSID_*`, `WIFI_PASSWORD_*`, `SERVER_URL_*`:
  ordered Wi-Fi profiles. The first successful profile sets the active backend
  `serverUrl`.
- `SPEAKER_VOLUME`: speaker output level.
- `MIC_SAMPLE_RATE`, `MIC_MAX_RECORD_SECONDS`, trigger/silence RMS thresholds,
  and pre-trigger buffer size: microphone capture behavior.
- `DISPLAY_BRIGHTNESS`: CoreS3 display brightness.

## Device HTTP API

The firmware exposes an HTTP API on port 80.

| Method | Path | Purpose | Notes |
| --- | --- | --- | --- |
| `POST` | `/play` | Queue a WAV URL for playback | Body: `{"voice_url":"http://..."}` |
| `POST` | `/mode` | Switch recording behavior | Body: `{"mode":"api"}` or `{"mode":"mcp"}` |
| `GET` | `/audio/status` | Check recording state | Returns `ready` and `mode` |
| `GET` | `/audio` | Fetch latest WAV recording | Consumes and clears readiness |
| `POST` | `/move` | Move head servos | Body: `{"x":0,"y":0,"speed":50}` |
| `POST` | `/home` | Return head to home position | Servo must be ready |
| `POST` | `/nod` | Nod gesture | Servo must be ready |
| `POST` | `/shake` | Shake gesture | Servo must be ready |
| `GET` | `/servo/status` | Servo diagnostics | Includes last command and feedback |
| `POST` | `/face` | Set face expression | Body: `{"face":"calm"}` |
| `GET` | `/face` | Read current face expression | Returns current face name |
| `GET` | `/snapshot` | Capture camera image | Returns 320x240 JPEG |

Supported face names are `calm`, `thinking`, `happy`, `sleepy`, `shy`, `smug`,
and `pouty`.

Be careful with `GET /audio`: it returns the current WAV recording and marks it
as no longer ready. Use `GET /audio/status` first when checking live devices.

## Safe Live-Device Checks

Set `STACKCHAN_IP` to the current device address before running these:

```sh
curl -sS --max-time 5 "http://$STACKCHAN_IP/audio/status"
curl -sS --max-time 5 "http://$STACKCHAN_IP/face"
curl -sS --max-time 5 "http://$STACKCHAN_IP/servo/status"
curl -sS --max-time 10 -o /tmp/stackchan_snapshot.jpg "http://$STACKCHAN_IP/snapshot"
```

Avoid `GET /audio` unless the task explicitly needs to consume the pending
recording.

## Audio Flow

Playback is push-based:

1. A host or MCP tool generates a WAV file and serves it over HTTP.
2. The host sends `POST /play` to Stack-chan with the `voice_url`.
3. The firmware enqueues an `AudioTask`.
4. `playback_service.cpp` downloads audio on a FreeRTOS task so the main loop
   stays responsive.
5. The main loop starts speaker playback after the download is ready.
6. Lip sync reads PCM amplitude from the WAV data and toggles mouth state.
7. Playback completion stops the speaker path and allows microphone resume.

The playback path expects WAV data suitable for the device. The MCP server
converts generated TTS to 24 kHz, mono, signed 16-bit WAV.

## Microphone Modes

The microphone service records 16-bit mono WAV with a pre-trigger ring buffer.
It uses RMS thresholds to trigger recording and to end after silence.

API mode:

- The device stores the latest recording.
- It posts WAV audio to `serverUrl + "/speech/transcribe"`.
- On a successful transcript, it sends the transcript into `chat_service`.

MCP mode:

- The device stores the latest recording.
- It skips device-side transcription.
- MCP clients can poll `/audio/status` and then fetch `/audio`.

Switch mode with:

```sh
curl -sS -X POST "http://$STACKCHAN_IP/mode" \
  -H "Content-Type: application/json" \
  -d '{"mode":"mcp"}'
```

## MCP Server

`mcp-server/server.py` exposes Stack-chan as MCP tools:

- `stackchan_say(text, lang="zh")`
- `stackchan_listen(lang="zh")`
- `stackchan_move(x=0, y=0, speed=50)`
- `stackchan_nod()`
- `stackchan_shake()`
- `stackchan_home()`
- `stackchan_face(expression="calm")`
- `stackchan_see()`
- `stackchan_status()`

Important environment variables:

- `STACKCHAN_IP`: device IP address. The code default is `10.83.20.187`.
- `STACKCHAN_PORT`: device HTTP port, usually `80`.
- `MAC_IP`: host IP used in generated audio URLs.
- `AUDIO_SERVE_PORT`: local HTTP port used to serve generated WAV files.
- `TTS_ENGINE`: `fish-audio` or `edge-tts`.
- `FISH_AUDIO_KEY`: required for Fish Audio TTS/ASR.
- `EDGE_TTS_BIN`: path to `edge-tts` when using the edge TTS fallback.

The server writes generated and captured media under `/tmp/stackchan_audio`.

Run in stdio mode:

```sh
python mcp-server/server.py
```

Run in Streamable HTTP mode:

```sh
python mcp-server/server.py --http --port 8002
```

Or use:

```sh
./start-http.sh
./start-http.sh stop
```

`start-http.sh` starts the MCP server on port `8002`, starts `cloudflared tunnel
run` if needed, and checks the public MCP endpoint.

## Face Assets

Face PNG paths are hard-coded in `firmware/src/face_service.cpp` and must match
files under `firmware/data/`:

- `/A_calm_320x240.png`
- `/B_thinking_320x240.png`
- `/C_happy_320x240.png`
- `/D_sleepy_320x240.png`
- `/E_shy_320x240.png`
- `/F_smug_320x240.png`
- `/G_pouty_320x240.png`

After changing face assets, upload the SPIFFS image:

```sh
cd firmware
pio run -t uploadfs
```

The face service mounts SPIFFS, preloads all face PNGs into PSRAM, and draws
from memory for faster switching.

## Servo Notes

The servo service uses SCServo over UART1:

- UART: `UART_NUM_1`
- Baud: `1000000`
- TX pin: `6`
- RX pin: `7`
- Yaw servo ID: `1`
- Pitch servo ID: `2`

Command inputs are degrees:

- Yaw `x`: `-128` to `128`
- Pitch `y`: `0` to `90`
- Speed: `0` to `100`

Servo position conversion uses local calibration constants in
`firmware/src/servo_service.cpp`. Check `/servo/status` when debugging movement
or acknowledgements.

## Camera Notes

The CoreS3 camera is GC0308 at QVGA `320x240`. It does not produce hardware
JPEG in this path; the firmware captures RGB565 and converts frames with
`frame2jpg()`. `initCamera()` releases M5Unified's internal I2C bus because the
camera SCCB pins share GPIO 11 and 12.

## Development Guidelines

- Prefer small firmware changes that keep the main loop responsive.
- Avoid blocking work in `loop()`; use existing queues/tasks where possible.
- Preserve PSRAM allocation patterns for audio, face, and camera buffers.
- Update both firmware and `mcp-server/server.py` when changing HTTP contracts.
- Keep `firmware/data/` and `face_service.cpp` face filenames synchronized.
- Do not commit local secrets or Wi-Fi settings from `firmware/src/config.h`.
- Use `firmware/config.h.example` for documented defaults.
- Before live-device tests, prefer non-destructive status endpoints.
