# Booth Enable Behind Busy Audio Polling

## Report And Evidence

Booth enable, nod and snapshot were all reported as busy for several minutes
on a mobile connection. The report suggested repeated noisy captures, but the
exact failed HTTP response and active route were not preserved in this report.
Do not treat the suspected noise trigger as proven.

Source inspection distinguishes two layers. Firmware accepts `/booth` during
recording, stops capture and clears local recording buffers. The shared generic
`Device channel busy; command was not queued` response comes from the mini
relay's single-flight admission. Its periodic `/audio/status` and binary `/audio`
fetches can occupy that slot on a slow link. Prior admission offered no waiting
position, so explicit booth enable could repeatedly lose to polling.

## Change

Allow one exact, authenticated booth-enable request to reserve the next slot.
Wait for the active request's complete response; deny new ordinary requests
while the reservation is held. Keep the original deadline, fail on caller
disconnect or peer replacement, and never retry or carry the request into a
new connection. A disabled/invalid booth request cannot wait behind an owner.
Cleanup must never release the admission owned by the pre-existing request.

This only prioritizes the capture-stop command. It does not queue nod/snapshot,
interrupt transmission, change ASR, tune voices, or change booth state by itself.
Priority is per request: a legacy PCM utterance can lose its later segments to
409 while enable is reserved, although its active frame completes intact. The
current live audio transport is whole-utterance WAV, not segmented PCM.

## Verification And Rollback

Regression tests cover status and large binary audio ownership, repeated polling,
duplicate booth requests, strict boolean validation, timeout, cancellation,
malformed bodies, peer replacement and no replay. A real loopback WebSocket/HTTP
test disconnects a waiting caller, then verifies a subsequent booth enable is
sent once after the active response. No actual audio or camera content is used.
Review also added delayed-upload rejection for disable/invalid bodies that
arrived busy, plus an explicit PCM-segment interleaving regression.

Only `mcp_server.outbound_relay` needs reload after the relay is idle. No MCP,
voice consumer, conversation worker or firmware restart is necessary. Runtime
booth state is unchanged by this release. Rollback is the previous relay source
revision plus the same idle relay reload. Physical noisy-link acceptance remains
a separate test and must not be inferred from the loopback regression.
