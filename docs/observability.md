# Stack-chan Observability

This project does not ship a metrics backend. The practical observability layer
is a small set of safe HTTP probes, host logs, firmware serial logs, and JSON
fields that can be scraped by whatever monitoring stack a deployment already
uses.

## Safe Health Probes

Set `STACKCHAN_IP` to the current device address before probing firmware:

```sh
curl -fsS --max-time 5 "http://$STACKCHAN_IP/audio/status"
curl -fsS --max-time 5 "http://$STACKCHAN_IP/face"
curl -fsS --max-time 5 "http://$STACKCHAN_IP/servo/status"
curl -fsS --max-time 5 "http://$STACKCHAN_IP/playback/status"
```

These endpoints do not consume recordings. Avoid `GET /audio` unless the test
explicitly needs to fetch and clear the pending WAV.

Useful host-side probes:

```sh
./start-voice-upload.sh status
./start-voice-bridge.sh status
```

The upload receiver also exposes a JSON health endpoint:

```sh
curl -fsS --max-time 5 "http://127.0.0.1:8767/health"
```

## Logs

- MCP HTTP server: `${STACKCHAN_LOG_DIR:-/tmp}/stackchan_mcp_http.log`
- Cloudflared tunnel from `start-http.sh`: `${STACKCHAN_LOG_DIR:-/tmp}/cloudflared.log`
- Voice upload receiver: `${STACKCHAN_VOICE_UPLOAD_LOG:-/tmp/stackchan_voice_upload.log}`
- Voice bridge: `${STACKCHAN_VOICE_BRIDGE_LOG:-/tmp/stackchan_voice_bridge.log}`
- Firmware serial monitor: `cd firmware && pio device monitor -b 115200`

For PCM playback diagnosis, set `STACKCHAN_SAVE_PCM=1` to preserve the streamed
PCM under `/tmp/stackchan_audio/diag_<session>.pcm`.

## Metric Fields

`GET /playback/status` is the best firmware health snapshot. Watch:

- `playing`, `kind`, and `pcm_session` for stuck playback sessions.
- `queued_pcm_segments`, `queued_pcm_bytes`, and `audio_queue_depth` for queue
  growth.
- `mic_state` and `mic_resume_requested` for microphone recovery after
  playback.
- `gesture` for stuck servo gestures.
- `free_heap` and `free_psram` for memory pressure after repeated playback or
  capture cycles.

`GET /servo/status` is the best servo snapshot. Watch:

- `ready` and `last_command_ok`.
- `last_command_ms` for stale motion commands.
- yaw and pitch feedback fields when diagnosing movement.

The voice upload `/health` payload is the best host-side receiver snapshot.
Use it to verify whether inbox and frontend forwarding are enabled plus the
non-sensitive upload limits and maximum upload size. It intentionally omits
paths, session identifiers, titles, wake words, origins, and tokens.

## Alert Candidates

Keep alerts deployment-local. Good first alerts are:

- Firmware probes fail for more than one or two polling intervals.
- `/playback/status` reports growing PCM or audio queues while `playing` does
  not clear.
- `free_heap` or `free_psram` drops steadily across repeated playback cycles.
- Upload receiver `/health` fails while the public recorder URL is expected to
  be live.
- Upload logs show repeated `401` or `429` responses, which usually mean token
  mismatch or rate limiting.
- Frontend forwarding repeatedly returns `409 busy` beyond the configured retry
  window.

Do not alert on the content of transcripts, wake words, tokens, local IPs, or
other user-specific deployment details.
