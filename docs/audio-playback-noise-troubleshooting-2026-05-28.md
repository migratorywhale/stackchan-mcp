# Audio Playback Noise Troubleshooting Record - 2026-05-28

This document records the Stack-chan audio playback debugging session from
2026-05-28. It covers an intermittent failure where spoken output sometimes
became harsh crackling noise.

## Summary

Stack-chan audio output is push-based: the MCP server generates a WAV file,
serves it over HTTP, then sends `POST /play` with a `voice_url`. The firmware
downloads that WAV into PSRAM and calls `M5.Speaker.playWav()`.

The strongest defect found was in `firmware/src/playback_service.cpp`.
`downloadVoice()` allocated a buffer using `Content-Length`, attempted to read
that many bytes, but never verified that the full body had actually been read.
If Wi-Fi or HTTP closed early, the unread tail of the PSRAM buffer could still
be passed to `M5.Speaker.playWav()`, producing crackling noise.

The fix makes incomplete or invalid WAV downloads fail before playback. It also
parses the WAV `data` chunk instead of assuming a fixed 44-byte header.

## Symptoms

- Most generated TTS files played normally.
- Occasionally, playback became loud crackling or broken noise.
- The failure was intermittent, which pointed toward transfer or buffer-state
  behavior rather than a consistently bad TTS format.

## Investigation

The playback path was traced as:

1. `stackchan_say()` generates TTS on the MCP server.
2. The generated WAV is stored under `/tmp/stackchan_audio`.
3. The MCP server serves that file over HTTP.
4. `POST /play` sends the WAV URL to the firmware.
5. Firmware `downloadVoice()` downloads the file into PSRAM.
6. `checkPendingPlayback()` calls `M5.Speaker.playWav()`.

Generated WAV files left in `/tmp/stackchan_audio` were checked with `file` and
`ffprobe`. They were valid PCM files:

```text
RIFF (little-endian) data, WAVE audio, Microsoft PCM, 16 bit, mono 24000 Hz
codec_name=pcm_s16le
sample_rate=24000
channels=1
bits_per_sample=16
```

One inspected WAV also contained a `LIST` chunk before `data`:

```text
00000020: 0200 1000 4c49 5354 ... LIST
00000040: ... 6461 7461 ...       data
```

That was not the likely cause of the speaker crackle, because
`M5.Speaker.playWav()` can parse WAV structure itself. It did reveal a separate
firmware assumption: lip sync started at byte 44 instead of the actual `data`
chunk offset.

## Root Cause

The firmware accepted partial HTTP downloads as successful.

Before the fix, `downloadVoice()`:

- read `len = http.getSize()`;
- allocated `ps_malloc(len)`;
- filled bytes until the HTTP loop ended;
- returned `outSize = len` regardless of `bytesRead`.

If the connection ended before `bytesRead == len`, the remaining bytes were
whatever happened to be in PSRAM. The speaker then received a nominally full WAV
buffer containing corrupted tail data.

## Final Fix

The firmware now rejects unsafe playback input before it reaches the speaker:

- `downloadVoice()` initializes output pointers to null/zero.
- It rejects missing, zero, or oversized `Content-Length`.
- It tracks read progress and fails on early connection close or read timeout.
- It verifies `bytesRead == Content-Length`.
- It parses WAV chunks and requires PCM, mono, 24 kHz, 16-bit audio.
- It frees PSRAM on any failed download or invalid WAV.

Playback metadata is now derived from the parsed WAV:

- `dataOffset` drives the lip-sync PCM start position.
- `dataSize`, sample rate, and bytes per frame drive the playback deadline.
- `LIST` and other non-audio chunks no longer shift lip-sync reads.

## Verification

Firmware build succeeded:

```sh
cd firmware && pio run
```

Result:

```text
RAM:   [===       ]  28.4% (used 93212 bytes from 327680 bytes)
Flash: [==        ]  18.8% (used 1230165 bytes from 6553600 bytes)
========================= [SUCCESS] Took 10.42 seconds =========================
```

Useful runtime logs after the fix:

```text
[DOWNLOAD] Complete: bytes=<file size> data=<pcm bytes> offset=<data offset>
[DOWNLOAD] Incomplete read: got=<bytes read> expected=<content length>
[WAV] Unsupported: format=<n> channels=<n> rate=<n> bits=<n>
[PLAY] Refusing invalid WAV
```

## Reproduction And Regression Checks

Normal playback:

```sh
curl -sS -X POST "http://$STACKCHAN_IP/play" \
  -H "Content-Type: application/json" \
  -d '{"voice_url":"http://<MAC_IP>:5060/tts_example.wav"}'
```

Expected result:

```json
{"success":true}
```

For a negative test, serve a deliberately truncated WAV with a stale
`Content-Length` or close the HTTP connection early. Expected serial behavior is
an incomplete-read log and no speaker playback.

For non-WAV input, point `voice_url` at a small text or HTML file. Expected
serial behavior is a WAV validation error and no speaker playback.

## Notes For Future Debugging

- Keep the host output format as 24 kHz, mono, signed 16-bit PCM WAV unless the
  firmware playback contract is intentionally changed.
- Do not assume WAV PCM starts at byte 44; ffmpeg may add metadata chunks.
- Treat intermittent crackle as a possible transport or buffer integrity issue,
  not only as an audio-level or TTS issue.
- If crackle returns after this fix, capture serial logs around
  `[DOWNLOAD] Complete` and `[PLAY] Speaker started`, then compare the logged
  byte counts against the served file size on the host.

## 2026-05-29 Follow-up: MCP PCM Streaming Noise

After the WAV download fix, crackling still occurred during Fish Audio PCM
speech. Later follow-up work focused on the `/play/pcm` route rather than the
WAV `/play` route. The host now defaults to `STACKCHAN_AUDIO_MODE=wav` for
normal speech; use `auto` or `pcm` only when explicitly testing PCM transports.

Additional mitigations now in place:

- `STACKCHAN_AUDIO_MODE=auto|pcm|wav` can select PCM or WAV for isolation.
- `STACKCHAN_SAVE_PCM=1` saves the original Fish PCM stream under
  `/tmp/stackchan_audio/diag_<session>.pcm`.
- PCM streaming applies host-side gain and limiting before device playback:
  `STACKCHAN_PCM_GAIN` defaults to `1.0`, and `STACKCHAN_PCM_LIMIT` defaults to
  `0.90`.
- PCM segments are cut near a low-amplitude sample when possible
  (`STACKCHAN_PCM_ZERO_CROSS_WINDOW`, default `256`) and get a short de-click
  ramp at the next segment start (`STACKCHAN_PCM_DECLICK_SAMPLES`, default
  `64`).
- Firmware logs `/play/pcm` session, `seq`, byte count, final flag, queue
  result, and sequence gaps. Invalid `seq` values are rejected instead of being
  played.
- Firmware limits each `/play/pcm` request body to 128 KiB. Normal MCP segments
  are about 48 KiB, so oversized bodies are treated as malformed input rather
  than consuming large PSRAM buffers.
- Firmware retains the most recently finished playback buffer for one more
  completion cycle before freeing it, reducing the risk of freeing memory while
  `M5.Speaker.playRaw()` internals still reference it.
- Firmware now passes completed WAV downloads from the download task to the
  main loop with a FreeRTOS queue instead of a `volatile` flag plus shared
  pointers. The main loop refuses to start a downloaded WAV while another
  playback is active, so playback buffers are no longer replaced mid-playback.
- `M5.Speaker.playWav()` return values are checked, and failed speaker starts
  no longer mark playback as active.
- Wi-Fi reconnect handling in the main loop is non-blocking, avoiding the
  previous 5 second stall that could pause HTTP, playback completion, and
  microphone resume handling.
- A follow-up ownership audit found that failed `playRaw()` starts could leave
  the same PCM pointer owned by both `playback_service.cpp` and the HTTP/queue
  caller. The failure path now frees and clears the current PCM buffer inside
  playback service, while callers avoid freeing buffers already consumed by a
  speaker-start failure.
- PCM `seq` diagnostics now advance only after a segment is accepted by
  playback service. A new session no longer overwrites the diagnostic session
  until its first segment has been accepted, so a busy request from another
  session cannot corrupt the active stream's expected sequence number.
- Invalid MCP PCM tuning environment values fall back to documented defaults
  instead of aborting server import.

Recommended isolation sequence:

```sh
# Check whether the validated WAV path is clean.
STACKCHAN_AUDIO_MODE=wav ./start-http.sh

# Force PCM, save the original Fish PCM, and capture MCP + serial logs.
STACKCHAN_AUDIO_MODE=pcm STACKCHAN_SAVE_PCM=1 ./start-http.sh

# If PCM still clips, lower the conditioning values.
STACKCHAN_PCM_GAIN=0.55 STACKCHAN_PCM_LIMIT=0.80 \
  STACKCHAN_PCM_DECLICK_SAMPLES=128 ./start-http.sh
```

To inspect a saved raw PCM file on the host:

```sh
ffmpeg -f s16le -ar 24000 -ac 1 \
  -i /tmp/stackchan_audio/diag_<session>.pcm \
  /tmp/stackchan_audio/diag_<session>.wav
```
