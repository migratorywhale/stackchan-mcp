# macOS Local Network access from background services

## Symptom

The Stack-chan device is reachable from an interactive shell:

```bash
/usr/bin/curl -sS http://STACKCHAN_IP/audio/status
```

but the MCP server launched by `launchd` reports an error such as:

```text
[Errno 65] No route to host
```

This can happen when macOS Local Network privacy treats the Python interpreter
used by a LaunchAgent or LaunchDaemon differently from Terminal.

The important detail is responsibility tracking: launching `/usr/bin/curl`
from Python does not necessarily give the child process Terminal's permission.
macOS may still attribute the request to the long-running `python3.x`
executable.

## Verify the distinction

Run both probes from the same account that owns the LaunchAgent:

```bash
/usr/bin/curl -sS --max-time 5 http://STACKCHAN_IP/audio/status
PYTHON_BIN -c 'import requests; print(requests.get("http://STACKCHAN_IP/audio/status", timeout=5).text)'
```

Interpret the result as follows:

- If both fail, investigate routing, Wi-Fi, Tailscale, and the device address.
- If interactive curl succeeds but the background service fails, inspect Local
  Network privacy before changing firmware or transports.
- If interactive curl succeeds and an interactive Python request fails, the
  Python interpreter itself probably lacks Local Network permission.

## Check Local Network privacy

Open:

```text
System Settings -> Privacy & Security -> Local Network
```

Find the Python interpreter used by the service, for example `python3.14`, and
enable it. Then restart every already-running Python service so it receives the
new permission state.

On macOS 26.5.1 we observed this exact failure:

- interactive `/usr/bin/curl` returned HTTP 200;
- both a LaunchAgent and a LaunchDaemon running as the logged-in user received
  curl error 7;
- `python3.14` was listed under Local Network with its toggle off;
- enabling the toggle and restarting the services made `stackchan_health`
  return `ok: true` immediately.

Apple's background is documented in
[TN3179: Understanding local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy).

## Curl transport

Set this in the private `.env` loaded by the Stack-chan MCP server:

```dotenv
STACKCHAN_HTTP_TRANSPORT="curl"
```

Restart the MCP service, then verify `stackchan_health`,
`stackchan_playback_status`, and one short `stackchan_say` call.

The curl transport invokes the fixed `/usr/bin/curl` path without a shell and
supports the same JSON and binary request bodies used by the normal device
client. The default remains `requests`. This transport does not bypass a denied
Local Network permission; fix the permission first.

## Headless Mac mini services

LaunchDaemon templates that still run the processes as the `koke` user live in:

```text
ops/launchd/xyz.migratorybird.stackchan-mcp.plist
ops/launchd/xyz.migratorybird.stackchan-voice-bridge.plist
```

Keep the equivalent user LaunchAgents unloaded or moved aside while these
Daemons are installed, otherwise login may start duplicate processes.

## Rollback

Remove the variable or set it back to:

```dotenv
STACKCHAN_HTTP_TRANSPORT="requests"
```

Then restart the background service. No device firmware change is required.
