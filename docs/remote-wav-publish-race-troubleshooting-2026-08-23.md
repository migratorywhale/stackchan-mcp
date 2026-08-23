# Remote WAV publish race (2026-08-23)

## Symptom

`stackchan_say` intermittently returned:

```text
Play was queued but playback did not start: kind=idle playing=False current_bytes=0
```

The firmware was responsive, `download_in_flight` was false, and the microphone
continued recording. Restarting the device was not required.

## Evidence

The MacBook audio server log showed the device requesting the exact generated
filename and receiving `404 File not found`. The file appeared on the MacBook
later with a matching hash. The mini was copying `tts_*.wav` with a separate
two-second polling loop, while the MCP sent `/play` immediately after TTS
generation.

The MacBook wait server could delay a missing-file response for eight seconds,
but that only reduced the race. It did not establish that the current file had
finished publishing, and the MCP playback-start timeout was shorter than the
wait window.

## Root cause

WAV publication and `/play` were independent asynchronous paths:

1. MCP generated a local WAV.
2. A polling LaunchAgent eventually noticed and rsynced it to the MacBook.
3. MCP immediately told Stack-chan to download the MacBook URL.

When step 3 overtook step 2, the MacBook returned 404 and the firmware safely
returned to idle.

## Fix

When `STACKCHAN_AUDIO_PUBLISH_TARGET` is configured, the MCP now rsyncs only the
current WAV and waits for successful completion before sending `/play`. Rsync's
normal temporary-file rename keeps the destination atomic. Publication has a
bounded timeout and failures are returned before any playback request is sent.

The old `xyz.migratorybird.audio-push` polling LaunchAgent is disabled in the
two-host deployment to avoid duplicate concurrent transfers. Its script remains
available as a rollback mechanism.

## Verification

- Python MCP tests: `95 passed`.
- Ruff checks passed for the changed Python files.
- A live short TTS request published the same SHA-256 on mini and MacBook.
- MacBook `5061` log returned HTTP 200 for the new filename.
- Device `started_ms` advanced and the microphone resumed recording afterward.
