# Voice bridge duplicate consumer troubleshooting (2026-08-10)

## Symptom

Physical Stack-chan wake phrases became intermittent after the host services were
reloaded. Some recordings transcribed correctly but failed to reach the frontend;
others produced empty or low-quality transcripts.

## Evidence

- Two long-running `stackchan_voice_bridge.py` processes were present. One had
  started on 2026-07-28; the system LaunchDaemon started another on 2026-08-10.
- Both processes wrote to the same `stackchan-voice-bridge.log` and polled the
  same device.
- `GET /audio` consumes and clears Stack-chan's current recording, so concurrent
  pollers race for each recording.
- The older process predated relay-token refresh support. Log entries showed valid
  wake-word matches followed by HTTP 401 responses from the frontend wake route.
- Binding the separate upload receiver to `0.0.0.0` only changed reachability. It
  did not resample or otherwise transform audio.

## Root cause

The migration left a manually started voice bridge alive while launchd also owned
a bridge. PID-file checks in `start-voice-bridge.sh` did not protect direct Python
launches or the system LaunchDaemon path.

## Fix

The stale process was terminated, leaving the system LaunchDaemon as the sole
consumer. The Python bridge now takes a target-specific host-local advisory lock
under the current user's `Library/Caches/stackchan/` directory before any mode
that can consume `GET /audio`. A second long-running instance remains in standby
and can take over after the active owner exits, but it cannot read recordings
concurrently. A contending `--once` probe reports `busy` and exits immediately;
read-only `--dry-run` probes do not take the consumer lock.

## Verification

```sh
ps -axo pid,ppid,lstart,command | grep stackchan_voice_bridge.py
curl -sS --max-time 5 http://STACKCHAN_IP/playback/status
uv run pytest tests/test_mcp_server.py -k voice_bridge
uv run ruff check scripts/stackchan_voice_bridge.py tests/test_mcp_server.py
```

At runtime, verify there is one active bridge process, `mic_running` is true, and
`mic_frame_count` continues increasing. A deliberate second bridge should emit a
single `standby` event and must not consume `/audio`.
