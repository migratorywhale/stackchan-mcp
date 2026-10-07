# Outing Mode

The user-facing name is now "出门模式" (outing mode), formerly "摆摊模式"
(booth mode). This is a naming change only: the MCP identifier, `/booth` routes,
`booth_mode` status field, and NVS storage keys remain unchanged for compatibility.
Existing clients can keep using `stackchan_booth_mode`; refreshed MCP tool metadata
advertises the new display name. No firmware flash or setting change is needed.

Outing mode makes the body output-only for audio. Touches give local happy-face
and head feedback when playback is idle; they do not start recording or generate
host pet notifications. The speaker still works. Input can come from the existing
phone frontend instead of the body's microphone. Camera behavior is unchanged.

## Control

The MCP tool `stackchan_booth_mode` accepts an optional `enabled` boolean:

- Omit `enabled` to read status without changing anything.
- `enabled=true` stops body microphone capture and clears its current recording,
  pre-trigger buffer, asynchronous capture frame and stored WAV.
- `enabled=false` restores normal touch/VAD capture. If speech is still playing,
  microphone recovery waits until playback finishes.

Both direct HTTP and outbound transport use `GET /booth` and `POST /booth`.
POST requires exactly `{"enabled":true}` or `{"enabled":false}`. State responses
include `booth_mode`, `persisted`, `capture_allowed`, `mic_running` and `success`.
The existing `/mode` endpoint remains a recording-buffer reset, not this switch.

The tool sets an explicit value; it does not toggle or automatically retry a
write. A lost response does not prove the setting was unchanged. Query it before
deciding what to do next. Unsupported old firmware returns an error, not a fake
successful mute.

### Outbound Priority Admission

The mini relay reserves at most one pending explicit `POST /booth` enable
(`{"enabled":true}`). While the current request finishes, new polling and other
commands receive the usual not-queued 409, so polling cannot win the next slot
repeatedly. The complete current response, including any audio binary payload,
must arrive before outing mode enable is sent. This is priority admission, not
preemption of an in-flight transfer or a firmware command queue.

Priority is per request, not per multi-request utterance. With legacy segmented
PCM playback, the current segment finishes but later segments can receive 409
and the speech client may abort the rest of that utterance. No segment is
automatically retried. The current deployment uses whole-utterance WAV playback.

Upload, waiting and execution share the existing 20-second command deadline.
A waiting enable expires or fails when its caller disconnects or its device
connection changes. It is never persisted or replayed after reconnect. There
is no new retry of a dispatched write. Queries and disabling outing mode do
not wait for priority, and an offline device still cannot receive the command.
Non-enable bodies that arrived busy remain rejected even if the active request
finishes while that body uploads.

Authenticated relay status now includes `pending_path` and
`booth_priority_reserved` to distinguish transport contention from microphone
state. Only the fixed route path is exposed, not request bodies or audio.

The current outing-mode firmware already accepts an enable while recording and
cancels local capture when it executes. A transport reservation cannot resolve
an offline link or force a blocked firmware handler to finish; the existing
partial-transition and persistence checks still apply. This priority change
requires only an idle reload of the mini outbound relay, not a firmware flash.

## Persistence And Failure Behavior

The setting is stored in device NVS (`sc_booth/enabled`); it survives reboot and
power changes. A new device with no setting retains normal behavior. An unreadable
or invalid setting blocks capture until it can be explicitly set successfully.
The microphone initialization, capture, touch-admission and post-playback recovery
paths all obey the setting. Playback finishing must not unmute outing mode.

Enabling blocks software capture even if stopping hardware or saving NVS fails,
but returns failure in that case. The main loop retries a failed microphone stop.
Cleanup waits for the audio gate even after the running flag is cleared, so a
concurrent playback shutdown cannot leave a late capture frame behind. Queries
also report an incomplete transition until that cleanup finishes.
Do not report a completed durable mute unless `booth_mode=true`, `persisted=true`,
`capture_allowed=false` and `mic_running=false`. Disabling is not applied when
the persistence write fails. Repeating an already-persisted setting does not
rewrite NVS.

Audio already downloaded by a host before enabling cannot be recalled by this
device setting. Outing mode does not delete conversation history or change the
phone microphone. It also does not fix the cause of a sensor false activation.

## Validation And Rollback

Native tests exercise the real booth/microphone implementation with hardware and
NVS stubs: capture cancellation, cleared buffers, touch/VAD guards, reboot,
playback recovery, persistence failure, and audio-gate failure. Separate tests
exercise real touch handling for local-only feedback and normal-mode regression.
Python tests cover MCP/client validation and the relay routes.

Before enabling on a real body, provision and build the matching firmware, reload
host services only when idle, then verify query -> enable -> touch/no recording ->
speech/no mic restart -> reboot/still muted -> disable/normal recording. Public
example builds are compile checks, not provisioned firmware to upload.

Disabling through this tool is the normal rollback. Older firmware does not know
this setting and may start its microphone even if NVS still contains booth=true;
do not roll firmware back while relying on outing mode for an output-only device.
