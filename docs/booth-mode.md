# Booth Mode

Booth mode makes the body output-only for audio. Touches give local happy-face
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

## Persistence And Failure Behavior

The setting is stored in device NVS (`sc_booth/enabled`); it survives reboot and
power changes. A new device with no setting retains normal behavior. An unreadable
or invalid setting blocks capture until it can be explicitly set successfully.
The microphone initialization, capture, touch-admission and post-playback recovery
paths all obey the setting. Playback finishing must not unmute booth mode.

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
device setting. Booth mode does not delete conversation history or change the
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
do not roll firmware back while relying on booth mode for an output-only device.
