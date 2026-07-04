# PCM Playback Stutter Troubleshooting - 2026-07-04

## Symptoms

- Stack-chan speech intermittently cut out or arrived with long gaps when using
  Fish Audio PCM streaming through `POST /play/pcm`.
- Device status after playback was healthy: playback idle, empty queues, no
  audio gate failures, and enough heap/PSRAM.

## Investigation

- Non-destructive device checks:

```sh
curl -sS --max-time 5 http://$STACKCHAN_IP/playback/status
curl -sS --max-time 5 http://$STACKCHAN_IP/audio/status
curl -sS --max-time 5 http://$STACKCHAN_IP/face
```

- `/playback/status` showed `queued_pcm_segments=0`, `audio_queue_depth=0`,
  `download_queue_depth=0`, and no `audio_gate_failed_acquire_count`.
- Short direct PCM tests completed, but first segment posting took around
  6.3-6.5 seconds.
- A longer direct PCM test initially failed before playback with:

```text
invalid PCM payload size: 629
```

- After fixing odd byte chunk handling locally, longer PCM tests completed but
  still took far longer than audio duration:

```text
segments=8 bytes=383266 timing[pcm_total=45361ms first_segment_posted=6936ms]
segments=10 bytes=481348 timing[pcm_total=57735ms first_segment_posted=8289ms]
segments=11 bytes=512556 timing[pcm_total=62598ms first_segment_posted=7524ms]
```

- 24 kHz mono s16le PCM requires about 48 KB/s for real-time playback. The
  observed host-to-device PCM push path was closer to 8 KB/s in these tests.

## Root Cause / Current Best Hypothesis

There are two separate issues:

1. Fish Audio PCM stream chunks are not guaranteed to align to 16-bit sample
   boundaries. The host code assumed each chunk length was even, so odd chunks
   could abort PCM and trigger WAV fallback.
2. Even when chunk boundaries are handled, the current HTTP `POST /play/pcm`
   push path is too slow to sustain real-time playback for longer utterances.
   Firmware receives bounded PCM segments, but segment uploads complete slower
   than Stack-chan can play them, causing audible gaps.

This is not currently a heap, PSRAM, queue-depth, or audio-gate exhaustion
problem.

## Mitigation Applied

- Local `.env` was changed to:

```sh
STACKCHAN_AUDIO_MODE="wav"
```

- MCP server was restarted with `./start-http.sh stop && ./start-http.sh`.
- WAV path test completed quickly and played continuously:

```text
Fish Audio WAV/zh mode=wav timing[tts=1653ms validate=0ms status=268ms play_post=60ms playback_wait=1108ms say_total=3089ms]
```

## Code Fix

- Host PCM upload now carries a trailing partial sample byte across Fish stream
  chunks instead of rejecting odd-length chunks immediately.
- A configurable `STACKCHAN_PCM_FIRST_SEGMENT_TIMEOUT(_SEC)` was added as a
  guard for slow pre-playback PCM startup, though it does not solve slow segment
  upload after playback has begun.
- Follow-up firmware and host changes now provide non-WAV PCM paths for
  experiments: TCP sends raw 24 kHz mono s16le over a reliable socket, and UDP
  sends framed datagrams. The normal MCP speech path remains WAV until audible
  PCM playback is verified on the device.

## Follow-up Options

- Use `STACKCHAN_AUDIO_MODE=wav` for normal speech. Use `auto` only when testing
  PCM with WAV fallback, `pcm` to force PCM without fallback, `tcp`/`staged` to
  isolate PCM transport behavior, or `udp` to require the experimental UDP
  stream.
- If PCM is kept for experiments, add per-segment post timing telemetry to
  confirm whether bottlenecks are Fish streaming, HTTP upload, or firmware
  queue handling.
