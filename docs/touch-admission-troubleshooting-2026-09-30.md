# Touch Admission During Ambient Recording

## Symptom And Evidence

During outbound-channel acceptance, a short touch produced no visible new
recording feedback. The sensor was available, not suspended, and had zero
camera-resume failures; the camera session was inactive.

The live touch counters showed 9 recording requests, including 6 rejected
starts. Playback was idle, the microphone was running without frame failures,
and its state was recording. Recent bridge receipts had voice source rather
than touch source and were correctly not forwarded without a wake word.
These counters establish rejected touches, not a timestamped trace of every
individual reported tap.

## Cause And Change

`requestTouchRecording()` previously accepted only idle/triggering states,
not a voice recording already started by ambient sound. This guard predates
the outbound transport. It can explain an apparently unresponsive touch in
a noisy room; the sensor and WSS delivery are separate stages.

Allow an explicit touch to replace only a VOICE recording. Reuse
`beginRecording(..., TOUCH, false)` to clear sample and pre-trigger buffers,
voice confirmation, silence timer, and start time. Do not merely relabel the
earlier audio as touch: that would forward pre-touch conversation without a
wake word.

Resource, microphone-running, speaker and PCM-stream guards remain unchanged.
An existing TOUCH recording is not restarted by a second tap, and SENDING or
an unknown recording source cannot be replaced. New read-only playback status
fields expose the current source and successful touch trigger count.

## Verification

Six native policy cases cover idle, triggering, active voice takeover, repeated
touch, unknown source, and sending. The full native suite passed 67/67.
Private/public firmware builds and physical retest are recorded in the local
deployment handoff; do not infer hardware acceptance from unit tests.

For a live retest, first record `/touch/status` and the relevant
`/playback/status` counters, then ask for one short touch and one test phrase.
The voice bridge is the sole `GET /audio` consumer. Check its new receipt for
touch source and actual frontend delivery without dumping unrelated transcripts.
