# WAV Download Recovery Troubleshooting - 2026-08-23

## Symptoms

- One interrupted WAV download could leave the audio pipeline silent.
- Later speech requests were accepted but never reached playback.
- A physical power cycle was the only known recovery.

## Investigation

- USB inspection identified the CoreS3 as an Espressif USB Serial/JTAG device
  at `/dev/cu.usbmodem1101`.
- The first MacBook checkout was an older firmware tree. Flashing it exposed
  repository drift because it used stale hard-coded Wi-Fi and PNG faces, while
  the deployed device had NVS Wi-Fi and GIF faces.
- The active source was recovered from the Mac mini checkout at
  `/Users/koke/Projects/stackchan-mcp`. A full 16 MB flash backup was saved on
  the MacBook before restoring the active firmware line.
- Source inspection found that `downloadTask()` used non-blocking completion
  queue writes. If the success or failure result was dropped,
  `s_downloadInFlight` remained true forever.

## Root Cause

The completion event both carries the downloaded buffer and releases the
single in-flight download state. Dropping that event permanently prevents later
WAV downloads from starting. The HTTP reader had an inactivity timeout but no
absolute transfer deadline or independent final recovery path.

## Fix

- Use a one-slot completion queue matching the one-download-in-flight invariant.
- Block the worker until the main loop accepts the one completion event.
- Fail initialization cleanly if either queue or the worker task is unavailable.
- Add a 10-second connect/inactivity timeout and a 30-second body-read limit.
- Expose `download_age_ms` and `download_watchdog_ms` through playback status.
- Restart automatically after 60 seconds only if the normal recovery path fails.

## Verification

- `cd firmware && pio run -e m5stack-cores3`: passed; RAM 36.1%, flash 21.6%.
- Flashed the live CoreS3 and confirmed NVS Wi-Fi, GIF faces, HTTP, microphone,
  speaker, and servo services were healthy.
- Served a WAV response declaring 12,044 bytes but closed after 6,022 bytes.
  Firmware logged the incomplete read and returned to
  `download_in_flight=false` with `download_age_ms=0`.
- Without restarting or unplugging, served the complete 12,044-byte WAV. It
  downloaded and played, released its buffer, and resumed the microphone.

## Recovery Artifact

The MacBook recovery directory contains the full post-incident flash backup and
the verified firmware images used for the restore. It is intentionally outside
the repository because it may contain device-local NVS state.
