# Stackchan Say No Audio Troubleshooting - 2026-07-03

## Symptoms

`stackchan_say` returned a successful MCP tool result, but Stack-chan did not
make audible speech. The device was reachable and idle:

```sh
curl -sS --max-time 5 "http://$STACKCHAN_IP/playback/status"
```

The response showed `playing:false`, `kind:"idle"`, and an unlocked audio gate.

## Investigation

Safe checks confirmed that the device HTTP server was reachable:

```sh
curl -sS --max-time 5 "http://$STACKCHAN_IP/face"
curl -sS --max-time 5 "http://$STACKCHAN_IP/playback/status"
```

The host audio server on port `5060` was serving `/tmp/stackchan_audio`, and
generated WAV files were valid 24 kHz mono signed 16-bit PCM. One generated file
had a non-silent signal:

```text
duration_s=5.848 peak=25840 rms=4770.7
```

Posting an older WAV directly to `/play` initially moved the device to
`playing:true kind:"wav"`, proving that the device could download from the host
and start playback. Later, repeated `stackchan_say` and direct `/play` requests
returned `{"success":true}` from `/play`, but `/playback/status` stayed idle.
The `deadline_ms` changed while `started_ms` did not, which means the firmware
parsed a downloaded WAV but did not successfully start `M5.Speaker.playWav()`.

## Root Cause

The firmware depended on `M5.Speaker.playWav()` to both parse and start
downloaded WAV playback. During the failure, the firmware's own WAV parser got
far enough to update `deadline_ms`, but `started_ms` did not change and
`playing` stayed false. This showed that `/play` accepted the queue request and
the WAV downloaded, but the speaker start failed afterward.

The first mitigation, using a fixed virtual speaker channel with replacement
enabled, did make one live playback start but did not consistently fix short
generated WAVs. The robust fix is to keep the firmware's explicit WAV
validation and then play the parsed PCM data with `M5.Speaker.playRaw()`, the
same M5Unified primitive used by the PCM endpoint.

## Fix

Firmware WAV playback now parses the downloaded WAV and starts the speaker from
the parsed PCM data:

```cpp
M5.Speaker.playRaw(
    (const int16_t*)(wav_data + data_offset),
    data_size / sizeof(int16_t),
    sample_rate,
    false,
    1,
    0,
    true
);
```

The MCP WAV path also records `/playback/status` before posting `/play` and then
waits for either `playing:true` or a changed `started_ms`. If the device accepts
the queue but playback never starts, `stackchan_say` now returns a failure with
runtime status instead of a false success.

## Verification

```sh
uv run ruff check mcp_server tests/test_mcp_server.py
uv run pytest tests/test_mcp_server.py
cd firmware && pio test -e native
cd firmware && pio run -e m5stack-cores3
```

Results:

- `ruff`: passed.
- `tests/test_mcp_server.py`: 47 passed.
- Native firmware tests: 10 passed.
- CoreS3 firmware build: success.
- Firmware upload to `/dev/cu.usbmodem101`: success.

## Live Verification

The updated firmware was flashed with:

```sh
cd firmware
pio run -e m5stack-cores3 -t upload --upload-port /dev/cu.usbmodem101
```

After upload, active MCP verification with a short utterance returned success
and `/playback/status` showed:

```text
playing=true kind=wav current_bytes=69040 started_ms=15701
```
