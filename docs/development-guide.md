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
- `mcp_server/server.py`: Python MCP server that exposes Stack-chan tools and
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

## Quality Checks

The repository has a small shared quality-check entrypoint at the project root:

```sh
make lint
make test
```

Python host tooling uses `ruff` and `pytest` through `uv`:

```sh
uv run ruff check .
uv run pytest
```

MCP server tests are isolated from the live device and mock the MCP package at
import time, so they are safe to run without consuming `/audio` or calling the
Stack-chan HTTP API:

```sh
make test-mcp
```

Firmware linting uses PlatformIO's `cppcheck` integration:

```sh
cd firmware
pio check --severity=high --fail-on-defect=high
```

`make test` also builds the firmware with `pio run`, which is the practical
regression check for the Arduino/CoreS3 side of this project.

Use this matrix when choosing what to run:

| Change type | Minimum check | Broader handoff check |
| --- | --- | --- |
| Python or MCP server only | `uv run ruff check .` and `uv run pytest` | `make lint` |
| MCP tool behavior or guardrails | `make test-mcp` | `make lint` and `make test` |
| Firmware only | `cd firmware && pio run` | `make lint` and `make test` |
| HTTP contract shared by firmware and MCP | `uv run pytest` and `cd firmware && pio run` | `make lint` and `make test` |
| Face assets under `firmware/data/` | `cd firmware && pio run -t uploadfs` before device use | Document any filename/path changes |

The Python tooling is declared in `pyproject.toml`, locked by `uv.lock`, and
can also be installed with `requirements-dev.txt` for environments that do not
use `uv`.

`pio check` is intentionally configured as a high-severity gate. Cppcheck emits
medium/low warnings from bundled libraries and legacy SCServo driver code, so
the day-to-day lint target focuses on defects that should block handoff.

## Local Configuration

Create local firmware configuration from the example:

```sh
cp firmware/config.h.example firmware/src/config.h
```

Then edit `firmware/src/config.h` locally. Keep secrets out of commits.

Important configuration groups:

- `WIFI_NETWORK_COUNT`, `WIFI_SSID_*`, `WIFI_PASSWORD_*`: ordered Wi-Fi profiles.
- `SPEAKER_VOLUME`: speaker output level.
- `MIC_SAMPLE_RATE`, `MIC_MAX_RECORD_SECONDS`, trigger/silence RMS thresholds,
  and pre-trigger buffer size: microphone capture behavior.
- `DISPLAY_BRIGHTNESS`: CoreS3 display brightness.

## Device HTTP API

The firmware exposes an HTTP API on port 80.

| Method | Path | Purpose | Notes |
| --- | --- | --- | --- |
| `POST` | `/play` | Queue a WAV URL for playback | Body: `{"voice_url":"http://..."}` |
| `POST` | `/play/pcm` | Play raw PCM audio | Body is 24 kHz mono signed 16-bit little-endian PCM |
| `POST` | `/mode` | Clear stale recording state | Body: `{"mode":"mcp"}` |
| `GET` | `/audio/status` | Check recording state | Returns `ready` and `mode` |
| `GET` | `/audio` | Fetch latest WAV recording | Consumes and clears readiness |
| `POST` | `/move` | Move head servos | Body: `{"x":0,"y":0,"speed":50}` |
| `POST` | `/home` | Return head to home position | Servo must be ready |
| `POST` | `/nod` | Nod gesture | Servo must be ready |
| `POST` | `/shake` | Shake gesture | Servo must be ready |
| `GET` | `/servo/status` | Servo diagnostics | Includes last command and feedback |
| `GET` | `/playback/status` | Runtime diagnostics | Playback, PCM queues, mic state, gesture, heap, and PSRAM |
| `POST` | `/face` | Set face expression | Body: `{"face":"calm"}` |
| `GET` | `/face` | Read current face expression | Returns current face name |
| `GET` | `/snapshot` | Capture camera image | Returns 320x240 JPEG |

Supported face names are `calm`, `thinking`, `happy`, `sleepy`, `shy`, `smug`,
and `pouty`.

Servo gestures are non-blocking. `/nod` and `/shake` start a stepper that is
advanced from `loop()`, and direct `/move` or `/home` commands cancel any active
gesture before moving.

Be careful with `GET /audio`: it returns the current WAV recording and marks it
as no longer ready. Use `GET /audio/status` first when checking live devices.

## Safe Live-Device Checks

Set `STACKCHAN_IP` to the current device address before running these:

```sh
curl -sS --max-time 5 "http://$STACKCHAN_IP/audio/status"
curl -sS --max-time 5 "http://$STACKCHAN_IP/face"
curl -sS --max-time 5 "http://$STACKCHAN_IP/servo/status"
curl -sS --max-time 5 "http://$STACKCHAN_IP/playback/status"
curl -sS --max-time 10 -o /tmp/stackchan_snapshot.jpg "http://$STACKCHAN_IP/snapshot"
```

Avoid `GET /audio` unless the task explicitly needs to consume the pending
recording.

## Audio Flow

WAV playback is push-based:

1. A host or MCP tool generates a WAV file and serves it over HTTP.
2. The host sends `POST /play` to Stack-chan with the `voice_url`.
3. The firmware enqueues an `AudioTask`.
4. `playback_service.cpp` downloads audio on a FreeRTOS task so the main loop
   stays responsive.
5. The download task passes completed WAV buffers back to the main loop through
   a FreeRTOS queue; the main loop starts speaker playback only when no audio is
   already playing.
6. Lip sync reads PCM amplitude from the WAV data and toggles mouth state.
7. Playback completion stops the speaker path and allows microphone resume.

The WAV playback path expects data suitable for the device. The MCP server
converts generated TTS to 24 kHz, mono, signed 16-bit WAV.

For lower latency speech, firmware also accepts `POST /play/pcm` with raw PCM:

- Format: 24 kHz, mono, signed 16-bit little-endian.
- Content type: `audio/x-raw;format=s16le;rate=24000;channels=1`.
- MCP sends PCM in 48 KiB segments and firmware accepts later segments with
  `202 Accepted` while the current PCM segment is playing.
- Each PCM segment includes `session`, `seq`, and `final` query parameters.
  Firmware only queues segments for the active PCM session and logs the session
  id, segment size, final flag, and queued byte count. Missing, repeated, or
  skipped `seq` values are rejected and queued PCM is cleared. The expected
  sequence number advances only after the segment is accepted by playback
  service.
- The firmware stops the microphone, starts speaker playback with
  `M5.Speaker.playRaw()`, computes lip sync from PCM amplitude, and queues
  subsequent PCM segments while the current PCM segment is playing.
- Playback buffer ownership stays in `playback_service.cpp`. Finished buffers
  are retired for one additional completion cycle before being freed, so
  `M5.Speaker.playRaw()` / `playWav()` internals are not handed memory that has
  just been released.
- The main loop never blocks for Wi-Fi reconnect. `serviceWiFi()` requests
  reconnects at intervals while HTTP, playback, and microphone services keep
  running.
- After recording, the microphone service stores the generated WAV locally for
  MCP clients to fetch through `/audio`.
- Playback timeout clears any queued PCM segments before resuming normal audio
  queue processing, so stale segments from a broken stream are not replayed.
- The MCP server posts Fish PCM in bounded segments so playback can start
  before the full TTS response has completed. Each segment is still sent as a
  normal HTTP request with `Content-Length`, because the ESP32 `WebServer`
  handler is not compatible with chunked request bodies.
- The MCP server tries Fish Audio PCM streaming first when `TTS_ENGINE` is
  `fish-audio` and `FISH_AUDIO_KEY` is set. If streaming setup or playback
  fails before any PCM segment is accepted, it falls back to the existing WAV
  `/play` path. If a later PCM segment fails after audio has started, MCP
  returns an error instead of falling back to WAV to avoid duplicate speech.
- Set `STACKCHAN_AUDIO_MODE=wav` to force the validated WAV path, `pcm` to
  force PCM without fallback, or `auto` for the default behavior. Set
  `STACKCHAN_SAVE_PCM=1` to save streamed Fish PCM as
  `/tmp/stackchan_audio/diag_<session>.pcm` for offline inspection. PCM
  streaming applies conservative gain/peak limiting before sending to the
  device; tune with `STACKCHAN_PCM_GAIN` and `STACKCHAN_PCM_LIMIT`. It also
  chooses segment boundaries near low-amplitude samples and smooths a short
  ramp at PCM segment boundaries to reduce clicks from `playRaw()` transitions.

Safe playback smoke tests:

```sh
# Non-destructive device reachability check.
curl -sS --max-time 5 "http://$STACKCHAN_IP/face"

# MCP TTS path. With Fish Audio credentials this tries segmented PCM first;
# without them it uses the validated WAV fallback.
MAC_IP="$MAC_IP" STACKCHAN_IP="$STACKCHAN_IP" \
  uv run python -m mcp_server.server

# Force WAV for crackle/noise isolation.
STACKCHAN_AUDIO_MODE=wav MAC_IP="$MAC_IP" STACKCHAN_IP="$STACKCHAN_IP" \
  uv run python -m mcp_server.server

# Force PCM and save the exact Fish PCM stream for diagnosis.
STACKCHAN_AUDIO_MODE=pcm STACKCHAN_SAVE_PCM=1 MAC_IP="$MAC_IP" STACKCHAN_IP="$STACKCHAN_IP" \
  uv run python -m mcp_server.server
```

## Microphone Recording

The microphone service records 16-bit mono WAV with a pre-trigger ring buffer.
It uses RMS thresholds to trigger recording and to end after silence.

- The device stores the latest recording.
- MCP clients can poll `/audio/status` and then fetch `/audio`.

Switch mode with:

```sh
curl -sS -X POST "http://$STACKCHAN_IP/mode" \
  -H "Content-Type: application/json" \
  -d '{"mode":"mcp"}'
```

## MCP Server

`mcp_server/server.py` exposes Stack-chan as MCP tools:

- `stackchan_say(text, lang="zh")`
- `stackchan_listen(lang="zh")`
- `stackchan_move(x=0, y=0, speed=50)`
- `stackchan_nod()`
- `stackchan_shake()`
- `stackchan_home()`
- `stackchan_face(expression="calm")`
- `stackchan_see()`
- `stackchan_status()`
- `stackchan_playback_status()`

Important environment variables:

- `STACKCHAN_IP`: device IP address. The code default is `10.83.20.187`.
- `STACKCHAN_PORT`: device HTTP port, usually `80`.
- `MAC_IP`: host IP used in generated audio URLs.
- `AUDIO_SERVE_PORT`: local HTTP port used to serve generated WAV files.
- `TTS_ENGINE`: `fish-audio` or `edge-tts`.
- `FISH_AUDIO_KEY`: required for Fish Audio TTS/ASR.
- `EDGE_TTS_BIN`: path to `edge-tts` when using the edge TTS fallback.
- `STACKCHAN_AUDIO_MODE`: `auto`, `pcm`, or `wav`; useful for isolating PCM
  streaming noise from WAV playback behavior.
- `STACKCHAN_SAVE_PCM`: set to `1` to save streamed PCM diagnostic files.
- `STACKCHAN_PCM_GAIN`: PCM streaming gain before playback, default `0.75`.
- `STACKCHAN_PCM_LIMIT`: PCM streaming peak limit as full-scale ratio, default
  `0.90`.
- `STACKCHAN_PCM_DECLICK_SAMPLES`: samples smoothed at each PCM segment
  boundary, default `64`.
- `STACKCHAN_PCM_ZERO_CROSS_WINDOW`: samples searched before the target segment
  boundary for a quieter cut point, default `256`.
  Invalid PCM tuning values are ignored with a warning and replaced by these
  documented defaults.

The server writes generated and captured media under `/tmp/stackchan_audio`.

`stackchan_playback_status()` calls firmware `GET /playback/status` and is the
preferred live diagnostic for playback bugs because it does not consume
recordings and reports queue depth, PCM state, microphone state, gesture state,
heap, and PSRAM.

Run in stdio mode:

```sh
python -m mcp_server.server
```

Run in Streamable HTTP mode:

```sh
python -m mcp_server.server --http --port 8002
```

Or use:

```sh
./start-http.sh
./start-http.sh stop
```

`start-http.sh` starts the MCP server on port `8002`, starts `cloudflared tunnel
run` if needed, and checks the public MCP endpoint. It reads optional overrides
from project-root `.env`: `STACKCHAN_PORT`, `MCP_PYTHON`, `MCP_MODULE`,
`STACKCHAN_PUBLIC_MCP_URL`, and `STACKCHAN_LOG_DIR`. If `MCP_PYTHON` is unset it
uses `uv run python`, which avoids hard-coding a personal virtualenv path.

Run the MCP-only regression tests without a device:

```sh
make test-mcp
```

These tests verify the exported tool names and device-facing guardrails such as
servo input clamping, face validation, audio URL generation, and avoiding
`GET /audio` when `/audio/status` is not ready.

When adding MCP tools or changing arguments, update `tests/test_mcp_server.py`
with import-safe tests. Tests should mock network/device calls and must avoid
calling live Stack-chan endpoints.

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

The servo service uses the official `m5stack/StackChan-BSP` library instead of
directly driving SCServo from application code. This matters because the
official StackChan hardware requires BSP initialization for board-level support,
including servo power enable through the IO expander.

Application commands are still exposed as degrees:

- Yaw `x`: `-128` to `128`
- Pitch `y`: clamped to `5` to `85` to avoid the extreme vertical range
- Speed: `0` to `100`, mapped to the BSP `0` to `1000` range

The firmware converts API values to BSP motion units, where `10` equals
`1 degree`, then calls `M5StackChan.Motion`.

Key references:

- `firmware/src/main.cpp`: calls `M5StackChan.begin()` and
  `M5StackChan.update()`.
- `firmware/src/servo_service.cpp`: wraps `M5StackChan.Motion.move()`,
  `goHome()`, `moveX()`, and `moveY()`.
- `docs/servo-troubleshooting-2026-05-16.md`: record of the servo failure
  investigation and BSP migration.

## Camera Notes

The CoreS3 camera is GC0308 at QVGA `320x240`. It does not produce hardware
JPEG in this path; the firmware captures RGB565 and converts frames with
`frame2jpg()`. `initCamera()` releases M5Unified's internal I2C bus because the
camera SCCB pins share GPIO 11 and 12.

## Development Guidelines

- Prefer small firmware changes that keep the main loop responsive.
- Avoid blocking work in `loop()`; use existing queues/tasks where possible.
- Preserve PSRAM allocation patterns for audio, face, and camera buffers.
- Update firmware, `docs/http-api.md`, and MCP client tests when changing HTTP
  contracts.
- Keep `firmware/data/` and `face_service.cpp` face filenames synchronized.
- Do not commit local secrets or Wi-Fi settings from `firmware/src/config.h`.
- Use `firmware/config.h.example` for documented defaults.
- Before live-device tests, prefer non-destructive status endpoints.

## Troubleshooting Records

Keep durable records of non-trivial debugging sessions under `docs/` so later
work can start from evidence instead of memory.

Use this filename pattern:

```text
docs/<topic>-troubleshooting-YYYY-MM-DD.md
```

Each troubleshooting record should include:

- Symptoms and user-visible behavior.
- Reproduction or verification commands.
- Investigation steps, including false leads that were ruled out.
- Root cause, or the current best hypothesis if the issue is not fully solved.
- Final fix or workaround.
- Concrete verification results, such as HTTP responses, serial logs, build
  output, screenshots, image-diff numbers, or measured values.
- Links to relevant upstream documentation, source repositories, or local files.

When a troubleshooting session changes normal development practice, update this
guide in the relevant section and link to the detailed troubleshooting record.
