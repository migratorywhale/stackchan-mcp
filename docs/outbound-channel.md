# Outbound channel v1

This is an opt-in transport, not a replacement for the working LAN HTTP API.
The body connects to a fixed WSS endpoint over Wi-Fi, including a phone hotspot.
The home host remains online; a laptop is no longer needed beside the body.
The existing host voice bridge reads recordings through the same outbound socket.
The old design note's separate firmware voice-upload route is not present in the
current firmware; the phone upload service is a different path.

## Components

1. Firmware keeps one authenticated, CA-verified WebSocket to `/stackchan/ws`.
2. `python -m mcp_server.outbound_relay` listens on `127.0.0.1:8766`.
3. MCP selects `STACKCHAN_TRANSPORT=relay` and calls the loopback gateway.
4. Only `/stackchan/ws` is routed through the public reverse proxy. The local
   HTTP control API must not be added as an unrestricted public ingress.

The rehearsal deployment uses the existing website's TLS terminator and a
dedicated host-to-VPS SSH reverse forward bound to VPS loopback. Neither the
old Cloudflare configuration nor a laptop forwards this channel. The relay
and SSH forward have separate user LaunchAgents; neither replaces an existing
service. Configure a 60-second proxy read timeout with 10-second device heartbeats.

The existing direct transport remains the default. The travel script and its
configuration are not changed by installing or testing this implementation.

The firmware configuration fields are `STACKCHAN_OUTBOUND_ENABLED` (default 0),
`STACKCHAN_OUTBOUND_HOST`, `STACKCHAN_OUTBOUND_PORT` (443),
`STACKCHAN_OUTBOUND_DEVICE_TOKEN`, `STACKCHAN_OUTBOUND_CA_PEM`, and
`STACKCHAN_OUTBOUND_NTP_SERVER`. The host is a DNS name, not a URL; the path is
fixed. A missing/invalid CA or missing time synchronization never enables an
insecure TLS fallback. `pio run -e m5stack-cores3-public` uses only the committed
example configuration, even if a private `src/config.h` exists nearby. That
public build is for compile verification, not flashing onto a provisioned body.

MCP configuration selects `STACKCHAN_TRANSPORT=relay`,
`STACKCHAN_RELAY_URL=http://127.0.0.1:8766` and its local control token file.
Speech uses bounded staged PCM segments regardless of the old TCP/UDP/WAV
setting. WAV TTS is locally decoded before upload; the body never downloads a
host audio URL. Staged speech starts after its final segment has arrived, so
long utterances have higher first-audio latency than streaming playback.
Camera streaming/session controls and servo diagnostics are not implemented in
this first outbound version; they return errors rather than silently using LAN.

## Gateway contract

The device and the local controller have **different** bearer tokens. Each must
be at least 24 non-whitespace ASCII characters. They are supplied through
`STACKCHAN_RELAY_DEVICE_TOKEN_FILE` and `STACKCHAN_RELAY_TOKEN_FILE`, or the
corresponding environment variables without `_FILE`. Secret files belong outside
the repository. Tokens are never put into URLs, JSON envelopes or diagnostics.

- `GET /relay/status`: local-token authenticated channel health, with `online`,
  `busy` and `last_seen_age`. This does not probe the device's LAN IP.
- `/device/<path>`: local-token authenticated facade for supported device APIs.
- `GET`: `/status`, `/env`, `/face`, `/snapshot`, `/audio/status`, `/playback/status`, `/audio`.
- `POST`: `/mode`, `/face`, `/move`, `/home`, `/nod`, `/shake`, `/play/pcm`.
- `/play` URL pulling is not exposed here. `GET /audio` consumes a recording,
  exactly as on LAN; only the existing single-consumer voice bridge uses it.
  Health checks use `/audio/status` and never consume a recording.

The relay reserves one slot before reading a request body. Concurrent requests
get HTTP 409 and are not queued. An upload taking over five seconds gets 408;
if the body reconnects during upload, the command is not sent to the new
connection. Offline requests get 503. A 20-second overall command deadline gives 504
and closes the channel. A reconnect fails any old pending request. **No command
is automatically replayed**, because a lost reply does not prove that an action
did not execute. The process runs as one worker and keeps no durable command log.

## WebSocket wire format

The firmware supplies its device-only bearer token in the upgrade's
`Authorization` header. Within five seconds of connection it sends:

```json
{"type":"hello","v":1}
```

It then sends `{"type":"heartbeat"}` every ten seconds. After 35 seconds without
a device message the relay closes the connection. Application JSON messages have
an 8 KiB limit. Incoming PCM and JPEG replies have a 128 KiB limit, plus the
36-byte header. Only `/audio` WAV replies can reach 512 KiB.

Example command from relay to body:

```json
{"v":1,"id":"0123456789abcdef0123456789abcdef","expires_at_ms":1893456000000,"method":"POST","path":"/face","query":{},"body":{"name":"happy"}}
```

Example JSON response:

```json
{"id":"0123456789abcdef0123456789abcdef","status":200,"body":{"ok":true}}
```

`expires_at_ms` is mandatory and generated afresh from the gateway's remaining
request deadline. The body checks it against its synchronized UTC clock before
executing a command, in addition to its monotonic timeout and connection
generation. This rejects old TCP-delayed commands even if a disconnect has not
yet become visible on the device. Both machines need an accurate clock; the
timestamp above is an example, never a value to reuse for live requests.

PCM uses the same command envelope with `path` `/play/pcm`, the existing
`session`, `seq`, `final` query fields, and a positive `binary_size`. It is followed
by one binary message containing:

```text
SCB1 | request id (32 ASCII lowercase hexadecimal bytes) | PCM bytes
```

PCM is 24 kHz, mono, signed little-endian 16-bit. A practical segment is at most
48 KiB. HTTP `Content-Type` is `audio/x-raw;format=s16le;rate=24000;channels=1`.
The device's existing playback queue supplies backpressure.

JPEG responses use `status`, `binary_size`, `content_type: image/jpeg` in the
JSON response, then the same binary header and JPEG bytes. Both ID and length
must match the pending request; old or mismatched replies cannot finish a new
request. Invalid framing closes the channel.

Recording replies use `content_type: audio/wav` and the same binary envelope.
The device copies the recording before consuming it; allocation/size failures
leave it available. A lost connection after consumption can still lose a reply,
as with the existing HTTP download. There is no automatic destructive-read retry
and this version does not claim durable voice delivery across disconnects.

## Staged validation and rollback

Before enabling the route:

1. Run Python tests, native firmware tests, a firmware build, and the high-severity
   firmware check in an isolated worktree. Never load production `.env` for tests.
2. Preserve the installed firmware image and private configuration separately.
   Build the candidate with the real hostname, device token and correct CA only
   when deployment is approved. Do not use TLS `setInsecure` or token query strings.
3. Add the exact public WebSocket path to the reverse proxy, start the loopback
   relay, and check that unauthenticated and wrong-role requests are rejected.
4. Flash and test at the desk while the body and MCP are idle. Keep the direct
   HTTP route usable. Confirm actual speech, face, movement, picture, environment,
   and voice-bridge delivery to the current conversation.
5. Move only the body onto a phone hotspot. Repeat the same checks; disable the
   MacBook forwarding route so it cannot accidentally make the test pass.
6. Toggle the hotspot and verify reconnect, no old movement/speech replay, no
   accumulated PCM after an interruption, and responsive touch/microphone/display.
   Target first speech within five seconds and recovery within thirty seconds;
   these are live acceptance targets, not promises from the offline tests.

Roll back MCP with `STACKCHAN_TRANSPORT=direct`, reload it only when idle, and
return the body to home Wi-Fi or the existing laptop travel route. If firmware
behavior regresses, restore the saved image. Remove only the new proxy path and
the two dedicated relay/SSH jobs; do not stop shared services or the existing
voice-upload server. Explicitly switch transport before using the old travel
script, which predates `STACKCHAN_TRANSPORT`.

## Dependencies

The relay pins Starlette and Uvicorn to the versions already in the repository's
lock, and adds [websockets 15.0.1](https://github.com/python-websockets/websockets/releases/tag/15.0.1).
Firmware uses [arduinoWebSockets 2.7.2](https://github.com/Links2004/arduinoWebSockets/releases/tag/2.7.2).
Both added releases predate the repository's dependency cooldown. This is a
single-device channel, not a general-purpose reverse proxy or remote shell.

The existing AnyIO and PyJWT pins are also updated to 4.14.2 and 2.14.0 for
[GHSA-82r6-8w77-94w6](https://github.com/advisories/GHSA-82r6-8w77-94w6) and
[GHSA-w6j9-cwv2-h6wq](https://github.com/advisories/GHSA-w6j9-cwv2-h6wq), respectively.
This is a security-advisory cooldown exception in the candidate environment,
not an automatic upgrade of the running production service.
