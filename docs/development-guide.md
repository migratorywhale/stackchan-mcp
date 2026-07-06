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
uv run pyright
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
| Python or MCP server only | `uv run ruff check .`, `uv run pyright`, and `uv run pytest` | `make lint` |
| MCP tool behavior or guardrails | `make test-mcp` | `make lint` and `make test` |
| Firmware only | `cd firmware && pio run` | `make lint` and `make test` |
| HTTP contract shared by firmware and MCP | `uv run pytest` and `cd firmware && pio run` | `make lint` and `make test` |
| Face assets under `firmware/data/` | `cd firmware && pio run -t uploadfs` before device use | Document any filename/path changes |

The Python tooling is declared in `pyproject.toml`, locked by `uv.lock`, and
can also be installed with `requirements-dev.txt` for environments that do not
use `uv`.

`pyright` runs in basic mode against `mcp_server/` and `scripts/`. Coverage is
reported for the same host-side modules during `make test`, but there is no hard
coverage threshold yet.

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

For lower latency speech, firmware accepts raw PCM without using WAV as the
live transport:

- TCP PCM stream on port `9090`: the MCP server sends
  `STACKCHAN_PCM_STREAM/1 session=<id> rate=24000 channels=1 width=2\n`, waits
  for `OK\n`, then sends raw 24 kHz mono signed 16-bit little-endian PCM until
  TCP EOF. Firmware prebuffers about 120 ms, then writes 10 ms frames to an
  ESP-IDF I2S DMA backend configured for the CoreS3 speaker amplifier. This path
  is experimental until audible playback is verified on the device.
- UDP PCM session: the MCP server starts `POST /audio/session`, receives a
  session token and UDP port, then sends 10 ms `24 kHz` mono signed 16-bit
  little-endian PCM frames as `SCP1` datagrams. Firmware tracks sequence
  numbers, fills missing frames with silence, and writes frames through the same
  I2S DMA backend. This remains available for experiments where lossy low-level
  datagrams are acceptable.
- HTTP staged PCM through `POST /play/pcm?mode=staged`: MCP sends bounded
  segments with `X-Stackchan-Pcm-Mode: staged`; firmware joins them in PSRAM
  and starts playback only after the final segment. This remains the fallback
  PCM path when TCP cannot connect before playback starts.

The legacy immediate HTTP PCM segment path remains available for direct tests:

- Format: 24 kHz, mono, signed 16-bit little-endian.
- Content type: `audio/x-raw;format=s16le;rate=24000;channels=1`.
- MCP can send PCM in 48 KiB segments and firmware accepts later segments with
  `202 Accepted` while the current PCM segment is playing.
- Each PCM segment includes `session`, `seq`, and `final` metadata. MCP sends
  these as `X-Stackchan-Pcm-Session`, `X-Stackchan-Pcm-Seq`, and
  `X-Stackchan-Pcm-Final` headers because ESP32 raw upload handling can lose
  query parameters; query parameters remain accepted for compatibility.
  Firmware only queues segments for the active PCM session and logs the session
  id, segment size, final flag, and queued byte count. Missing, repeated, or
  skipped `seq` values are rejected and queued PCM is cleared. The expected
  sequence number advances only after the segment is accepted by playback
  service.
- The firmware stops the microphone, starts speaker playback with
  `M5.Speaker.playRaw()`, computes lip sync from PCM amplitude, and queues
  subsequent PCM segments while the current PCM segment is playing.
- Playback buffer ownership stays in `playback_service.cpp`. Finished buffers
  are freed only after the M5Unified speaker task has been synchronously ended,
  so `M5.Speaker.playRaw()` internals are not handed memory that has just been
  released while I2S is still active.
- The main loop never blocks for Wi-Fi reconnect. `serviceWiFi()` requests
  reconnects at intervals while HTTP, playback, and microphone services keep
  running.
- WAV playback treats an active device-side download as a pending playback, so
  additional `/play` requests are held in the logical audio queue instead of
  being dropped by the lower-level download queue.
- The logical WAV queue accepts up to 16 pending items. Additional `/play`
  requests return `503 {"success":false,"error":"play queue full"}`.
- Queued WAV items keep priority ordering; items with the same priority are
  played FIFO by an internal sequence number.
- After recording, the microphone service stores the generated WAV locally for
  MCP clients to fetch through `/audio`.
- Playback timeout clears any queued PCM segments before resuming normal audio
  queue processing, so stale segments from a broken stream are not replayed.
- The MCP server defaults to `STACKCHAN_AUDIO_MODE=wav`, using the validated
  `M5.Speaker` WAV path for normal speech.
- Set `STACKCHAN_AUDIO_MODE=auto` or `pcm` only for PCM experiments. In those
  modes the MCP server tries Fish Audio PCM first if `TTS_ENGINE` is
  `fish-audio` and `FISH_AUDIO_KEY` is set. In `auto` mode, if PCM setup or
  upload fails before audio starts, it falls back to the existing WAV `/play`
  path. If PCM fails after audio has started, MCP returns an error instead of
  falling back to WAV to avoid duplicate speech.
- `STACKCHAN_PCM_TRANSPORT=tcp` selects the TCP PCM stream when PCM mode is
  enabled. `auto` tries TCP, then staged HTTP PCM. Set
  `STACKCHAN_PCM_TRANSPORT=staged` to skip streaming transports, or `tcp`/`udp`
  to require a specific stream path. UDP is kept as an explicit experimental
  path and is not used by transport `auto`.
- Use `wav` for normal speech, `auto` for PCM with WAV fallback, or `pcm` to
  force PCM without fallback. Set
  `STACKCHAN_SAVE_PCM=1` to save streamed Fish PCM as
  `/tmp/stackchan_audio/diag_<session>.pcm` for offline inspection. PCM applies
  peak limiting with `STACKCHAN_PCM_GAIN` defaulting to `1.0` and
  `STACKCHAN_PCM_LIMIT` defaulting to `0.90`. It also chooses segment
  boundaries near low-amplitude samples and smooths a short ramp at PCM segment
  boundaries.

Safe playback smoke tests:

```sh
# Non-destructive device reachability check.
curl -sS --max-time 5 "http://$STACKCHAN_IP/face"

# MCP TTS path. By default this uses the validated WAV path.
MAC_IP="$MAC_IP" STACKCHAN_IP="$STACKCHAN_IP" \
  uv run python -m mcp_server.server

# Explicitly keep the stable WAV path for crackle/noise isolation.
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
- `scripts/stackchan_voice_bridge.py` is the host-side bridge for the physical
  Stack-chan input path. It polls this same path, prints JSONL transcripts, and
  can forward deliberate wake-word transcripts into a frontend agent-host's
  `/wake` endpoint when `STACKCHAN_FRONTEND_SESSION_ID` is configured. Use
  `--dry-run --once` to inspect readiness without consuming the recording
  buffer. It reads project-root `.env` without overriding already exported
  variables.
- `./start-voice-bridge.sh` runs the bridge in the background. Transcript
  events are appended to `STACKCHAN_VOICE_INBOX`, defaulting to
  `/tmp/stackchan_audio/voice_inbox.jsonl`.
- MCP clients can call `stackchan_voice_inbox` and
  `stackchan_voice_inbox_clear` to read or clear those background transcripts.
- `scripts/stackchan_voice_upload_server.py` is the push-mode host receiver for
  browser/phone/PWA inputs. It exposes `POST /voice/upload` for `audio/wav`
  clients, runs the same Fish ASR path, appends transcript events to the same
  inbox, and can forward one transcript into a frontend agent-host's `/wake`
  endpoint when `STACKCHAN_FRONTEND_SESSION_ID` is configured.
- `./start-voice-upload.sh` starts, stops, and health-checks that receiver. It
  reads project-root `.env` and respects `STACKCHAN_VOICE_UPLOAD_HOST`,
  `STACKCHAN_VOICE_UPLOAD_PORT`, `STACKCHAN_VOICE_UPLOAD_LOG`, and
  `STACKCHAN_VOICE_UPLOAD_PIDFILE`.
- Both voice paths read `AGENT_HOST_TOKEN` from `STACKCHAN_FRONTEND_ENV` when
  `STACKCHAN_FRONTEND_TOKEN` is unset. This avoids copying the frontend token
  into the Stack-chan repo.
- The voice paths intentionally do not guess the active frontend room. Use
  `STACKCHAN_FRONTEND_SESSION_ID=<uuid>` when the voice prompt should enter a
  specific frontend session; omit it for inbox-only capture.
- To avoid copying the wrong UUID, both voice paths can resolve a session from
  a compatible frontend `web-sessions.json`: set `STACKCHAN_FRONTEND_REGISTRY`,
  then use
  `STACKCHAN_FRONTEND_SESSION_ID=latest` for the latest non-archived session, or
  `STACKCHAN_FRONTEND_SESSION_TITLE="lab-room"` for the latest non-archived
  session whose title contains that text.
- If agent-host returns `409 busy`, the target session is currently generating.
  Configure `STACKCHAN_FRONTEND_RETRIES` and `STACKCHAN_FRONTEND_RETRY_DELAY`
  to retry instead of dropping the frontend injection; the transcript is still
  appended to the inbox first.
- Configure `STACKCHAN_VOICE_WAKE_WORDS` as a comma-separated activation list
  such as `小塔,机器人` when the receiver should only forward deliberate
  speech. A transcript without a wake word remains in the inbox but is not sent
  to the frontend.
- The upload receiver serves a minimal recorder page at `/`. Mobile browsers
  usually require HTTPS before `navigator.mediaDevices.getUserMedia` exists, so
  phone tests need a tunnel or another HTTPS wrapper. Prefer `tailscale serve`
  for this — see `docs/tailscale-deployment.md` — over a public tunnel. If using `cloudflared
  tunnel --url`, pass an empty config such as
  `--config /tmp/empty-cloudflared.yml`; otherwise the existing
  `~/.cloudflared/config.yml` named-tunnel ingress rules can intercept the
  quick tunnel and return Cloudflare 404.
- For phone tests, set `STACKCHAN_VOICE_UPLOAD_TOKEN`, open the recorder as
  `https://...trycloudflare.com/`, and enter the token in the page. The page
  sends it as `X-Stackchan-Upload-Token`; unauthenticated uploads receive HTTP
  401. Older `?token=...` links remain accepted for compatibility, but the page
  moves that token into `sessionStorage` and cleans the address bar.
- For daily phone use, point your own HTTPS route or reverse proxy at
  `http://localhost:8767` and set `STACKCHAN_VOICE_PUBLIC_URL` to that public
  recorder URL. If you use Cloudflare's default certificate coverage, keep the
  route shape compatible with the certificate you actually have.
- `STACKCHAN_VOICE_UPLOAD_RATE_PER_MINUTE` limits upload attempts per client IP;
  set it to `0` only for local debugging.
- `./start-voice-upload.sh status` checks the local receiver, public HTTPS
  route, frontend `agent-host`, launchd-managed `cloudflared`, resolved frontend
  session, and wake-word configuration.

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

- `STACKCHAN_IP`: device IP address. Set this explicitly in `.env`.
- `STACKCHAN_PORT`: device HTTP port, usually `80`.
- `MAC_IP`: host IP used in generated audio URLs.
- `AUDIO_SERVE_PORT`: local HTTP port used to serve generated WAV files.
- `TTS_ENGINE`: `fish-audio` or `edge-tts`.
- `FISH_AUDIO_KEY`: required for Fish Audio TTS/ASR.
- `EDGE_TTS_BIN`: path to `edge-tts` when using the edge TTS fallback.
- `STACKCHAN_AUDIO_MODE`: `wav` by default; use `auto` to try PCM with WAV
  fallback or `pcm` to force PCM without fallback.
- `STACKCHAN_PCM_TRANSPORT`: `tcp` by default when PCM mode is enabled; use
  `auto` for TCP/staged fallback, `staged` to force HTTP staged PCM, or
  `tcp`/`udp` to require a specific stream path. UDP is experimental and only
  used when explicitly selected.
- `STACKCHAN_PCM_STREAM_PORT`: TCP PCM port, default `9090`.
- `STACKCHAN_SAVE_PCM`: set to `1` to save streamed PCM diagnostic files.
- `STACKCHAN_PCM_GAIN`: PCM streaming gain before playback, default `1.0`.
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

`start-http.sh` starts the MCP server on port `8002`. It starts
`cloudflared tunnel run` only when `STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL=1`,
then checks the public MCP endpoint. It reads optional overrides from
project-root `.env`: `STACKCHAN_PORT`, `MCP_PYTHON`, `MCP_MODULE`,
`STACKCHAN_PUBLIC_MCP_URL`, `STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL`, and
`STACKCHAN_LOG_DIR`. If `MCP_PYTHON` is unset it uses `uv run python`, which
avoids hard-coding a personal virtualenv path.

For client-specific setup snippets and one-click install notes, see
`docs/mcp-client-setup.md`.

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
