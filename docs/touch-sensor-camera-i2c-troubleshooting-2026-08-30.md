# Touch Strip and Camera I2C Handoff

## Symptom

The top three-zone touch strip works in the official Stack-chan examples but
does not produce events in this firmware, or it stops working after a camera
snapshot. ENV readings may also fail after camera initialization.

## Root Cause

On M5Stack CoreS3, the GC0308 camera SCCB interface and the internal I2C bus
both use GPIO 11/12. The Si12T touch controller is on the internal bus at
address `0x68`. Keeping the camera initialized permanently releases the bus
owned by M5Unified, so touch and other internal I2C clients cannot coexist with
that camera lifecycle.

This is a bus-ownership problem, not a touch-gesture parsing problem.

## Implementation Contract

1. Touch owns the internal I2C bus during normal operation.
2. `GET /snapshot` suspends touch and releases `M5.In_I2C` immediately before
   camera initialization. If release fails, camera startup is aborted and the
   firmware attempts to restore the internal bus instead of driving both bus
   owners at once.
3. The camera captures RGB565, converts it to JPEG, and is deinitialized in the
   same request.
4. Every camera exit path calls `M5.In_I2C.begin()`, probes `0x68`, and reruns
   `M5StackChan.TouchSensor.begin()`.
5. A snapshot is reported as failed if the internal bus cannot be restored, or
   if a touch controller that was present before capture disappears afterward.
   A device that never had a detectable Si12T can still use the camera.
   `GET /touch/status` increments `resume_failure_count` when an expected
   recovery fails, so the failure is visible remotely. After a transient
   failure, the main loop retries the internal-bus and touch-controller setup
   every two seconds instead of leaving touch disabled until another snapshot
   or reboot.

The touch behavior follows the official Stack-chan pattern: opposite-direction
swipes within 1500 ms count as petting. The local response is deliberately
non-blocking and does not enqueue audio or make a network request.

A short tap has a separate local behavior: it starts a forced microphone
recording and stores the finished WAV with `source=touch`. The host voice bridge
is responsible for ASR and frontend delivery. It bypasses wake-word filtering
only for this explicit source and renders the prompt as
`[Stack-chan语音输入] （触摸）<transcript>`; ordinary ambient recordings keep
the existing wake-word requirement.

The same bridge observes the monotonic `touch_pet_count` returned by
`GET /audio/status`. The first value after bridge startup is only a baseline;
each later increment queues one `[Stack-chan触摸]` prompt to the frontend. The
bridge drains at most four prompts per poll and retains a bounded backlog of 32
to absorb ordinary host stalls without allowing a corrupted counter jump to
flood the frontend. A counter reset after firmware reboot is rebaselined instead
of replayed. Local happy-face and head-shake behavior remains unchanged.

## Host Verification

```bash
cd firmware
pio test -e native
pio run -e m5stack-cores3
pio check -e m5stack-cores3 --severity=high --fail-on-defect=high
```

## Device Regression Checklist

After flashing, run these checks in order:

1. Call `GET /touch/status`; expect `available: true`, `suspended: false`, and
   `resume_failure_count: 0`.
2. Touch each of the front, middle, and back zones. Their corresponding
   intensity should briefly become nonzero.
3. Tap once; the face should switch to listening and `last_event` should become
   `recording_started`. Speak without a wake word, then pause. `/audio/status`
   should become ready with `source: touch`.
4. Swipe in one direction; `last_event` should become `swipe_forward` or
   `swipe_backward`.
5. Swipe back in the opposite direction within 1500 ms. Stack-chan should show
   the happy face and shake its head; `pet_count` should increment.
6. Call `GET /snapshot` and verify that a JPEG is returned.
7. Repeat steps 1-5 after the snapshot. Touch must still respond and
   `resume_failure_count` must remain zero.
8. Call `GET /env` after the snapshot and verify that its previously available
   sensors still return readings.
9. Exercise microphone recording and audio playback once. Touch gestures must
   not interrupt either path; a pet detected while audio is busy increments
   `suppressed_pet_count` instead.
10. Repeat snapshots and touch checks at least five times to catch lifecycle
   leaks that a single pass misses.

## Official References

- [StackChan-BSP touch example](https://github.com/m5stack/StackChan-BSP/blob/main/examples/TouchSensor/TouchSensor.ino)
- [StackChan-BSP touch sensor class](https://github.com/m5stack/StackChan-BSP/blob/main/src/utils/touch_sensor/touch_sensor.cpp)
- [M5CoreS3 GC0308 bus release](https://github.com/m5stack/M5CoreS3/blob/main/src/utility/GC0308.cpp)
- [Upstream Stack-chan camera lifecycle](https://github.com/stack-chan/stack-chan/blob/develop/firmware/host/app/runtime-camera.ts)
- [Upstream Stack-chan petting behavior](https://github.com/stack-chan/stack-chan/blob/develop/firmware/host/app/default-behavior/on-context-created.ts)
