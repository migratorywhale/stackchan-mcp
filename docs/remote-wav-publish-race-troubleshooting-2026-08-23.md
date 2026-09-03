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

The old `<your-reverse-dns>.audio-push` polling LaunchAgent is disabled in the
two-host deployment to avoid duplicate concurrent transfers. Its script remains
available as a rollback mechanism.

If a transient Tailscale, SSH, or host-wake delay exhausts one publish attempt,
the MCP terminates the entire rsync/ssh process group and retries once by
default. This keeps the synchronous ordering guarantee without leaving an
orphaned transfer that may complete after the tool already reported failure.

## Verification

- Python MCP tests: `95 passed`.
- Ruff checks passed for the changed Python files.
- A live short TTS request published the same SHA-256 on mini and MacBook.
- MacBook `5061` log returned HTTP 200 for the new filename.
- Device `started_ms` advanced and the microphone resumed recording afterward.

## Follow-up: bounded publish timeout (2026-08-24)

At 00:36 JST, a later speech request generated a valid 405,636-byte WAV on the
mini but returned `audio publish timed out after 20s`. The MacBook HTTP log had
no matching request, so this failure happened before `/play` and was separate
from both the firmware download recovery and the earlier 404 race. A later
publish over the same route completed in 3.77 seconds with matching SHA-256
hashes on both hosts, supporting a transient SSH/Tailscale or host-wake delay
rather than a persistent path or credential failure.

The publisher now gives transient failures one bounded retry. Each attempt runs
rsync and ssh in a new process group; timeout cleanup terminates that entire
group before the retry, preventing a late orphan transfer from violating the
publish-before-play result. The retry count is configurable and clamped to at
most three.

Follow-up verification:

- Full Python suite: `104 passed`.
- Focused publish tests cover cleanup, retry success, and retry exhaustion.
- Ruff and Pyright passed.
- Publish-only live check: 405,636 bytes, matching SHA-256 across mini and
  MacBook; no device playback was triggered.
- MCP LaunchDaemon restarted with `publish_attempts=2`.
- The old `audio-push` job/process count is zero; one MacBook audio wait server
  remains active.
