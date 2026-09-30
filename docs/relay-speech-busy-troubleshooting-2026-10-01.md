# Relay Speech Admission Retry

## Symptom And Evidence

Speech returned HTTP 409 with `Device channel busy; command was not queued`.
A later manual retry succeeded. No overlapping deliberate body command was
reported. The voice bridge's periodic `/audio/status` is a plausible competitor,
but the production logs inspected did not establish which request held the slot.

`outbound_relay.py` emits this exact JSON error only before dispatch: the early
admission check, or the pending-command check immediately before constructing a
new command. No command ID or PCM frame is sent for either rejected request.
Device-generated 409 responses and unknown completion after a disconnect are
different cases. A generic retry on every 409 would be incorrect.

## Fix

`post_pcm_stream` in relay mode recognizes HTTP 409 with the exact JSON object
`{"error":"Device channel busy; command was not queued"}`. It waits 1.5 seconds
and resubmits that segment, with identical bytes, session, sequence and final
flag. Each utterance has one shared retry budget, including later segments.
It never regenerates TTS or restarts previously accepted segments.

The generic relay client, voice recording fetches, servos, direct transport and
the gateway are unchanged. Other errors, malformed responses, second refusals,
timeouts and unknown completion still stop without replay. This is a bounded
admission retry, not a speech-delivery guarantee or a queue.

## Verification And Release

The loopback WebSocket regression holds a status request open, observes the real
relay reject a speech request, then releases the status request. The retry reaches
the fake device exactly once. No real body or production listener is used.
Unit regressions cover both TTS paths, exact payload reuse, per-utterance budget,
failure paths and unchanged non-speech behavior.

This candidate changes host Python only; no firmware build or flash is needed.
Before release, confirm MCP is idle, preserve its deployed source revision,
apply the reviewed commit and reload only that MCP service. Do not restart the
relay, voice consumer, current conversation worker or body for this fix.
Rollback is the previous host source revision followed by an idle MCP reload.
No production reload or physical speech test was performed while preparing it.
