# Recommended: Reaching Stack-chan MCP over Tailscale

This is the recommended way to reach the Stack-chan MCP control endpoint (and,
optionally, the phone voice-upload receiver) from a device that isn't on the
same LAN. It replaces the old pattern of exposing a public `cloudflared`
tunnel and hoping the hostname stayed private.

## 1. Why Tailscale instead of a public tunnel

A public quick tunnel (`cloudflared tunnel --url ...` or a named tunnel
fronting `0.0.0.0`) puts an HTTP endpoint that can drive real device actions
(speak, look, move) on the public internet. The only thing standing between
"anyone on earth" and the device was the secrecy of a hostname. That secrecy
is not load-bearing in practice: hostnames leak through shell history that
gets pasted somewhere, screenshots, log aggregators, and — for named tunnels
in particular — Certificate Transparency logs, which publish every hostname a
public CA issues a certificate for, permanently and publicly searchable. A
tunnel hostname that appears once in a public git history or in a CT log is
burned forever.

[Tailscale](https://tailscale.com/) builds a private mesh network over
WireGuard between only the devices you enroll in your tailnet. There is no
public DNS record for a Tailscale node's `*.ts.net` hostname resolvable
outside the tailnet, and MagicDNS names are not the subject of publicly
issued, CT-logged certificates the way a public tunnel hostname is (see
[`tailscale serve`](#3-phone-microphone--voice-upload-the-https-catch) below
for the one case where a real certificate is involved, and why that's safe).
Reachability is enforced by tailnet membership and ACLs, not by whether an
attacker happens to guess or find a URL.

In short: with a public tunnel, hostname secrecy is the entire security
model, and it fails. With Tailscale, there is no public endpoint to find in
the first place.

## 2. Binding the MCP server

The MCP HTTP server reads two environment variables at startup
(`mcp_server/server.py`, `parse_args`):

- `STACKCHAN_MCP_HTTP_HOST` — interface to bind. **Default: `127.0.0.1`.**
- `STACKCHAN_MCP_HTTP_PORT` — port to bind. **Default: `8002`.**

There are two supported ways to reach that server over Tailscale:

### 2a. Recommended: keep it on loopback, front it with `tailscale serve`

Leave `STACKCHAN_MCP_HTTP_HOST` at its default (`127.0.0.1`) — do not set it
at all — and let `tailscale serve` reverse-proxy tailnet traffic to the
loopback port:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8002
```

(Check `tailscale serve --help` for the exact flags on your installed
version; the shape above is illustrative.)

With this setup the MCP process itself never listens on any routable
interface — not LAN, not the tailnet IP, nothing but loopback. Only the
`tailscaled` daemon, which enforces tailnet ACLs, terminates connections from
the network. This is the smallest attack surface available and the
recommended default.

Your MCP client then connects to:

```text
https://your-machine.your-tailnet.ts.net:8443/mcp
```

### 2b. Alternative: bind directly to the tailnet IP

If you'd rather skip the `tailscale serve` proxy hop, you can bind the MCP
server straight to the node's tailnet address:

```bash
export STACKCHAN_MCP_HTTP_HOST="100.x.y.z"   # this node's tailnet IP
./start-http.sh
```

Your MCP client then connects to:

```text
http://100.x.y.z:8002/mcp
```

**Do not set `STACKCHAN_MCP_HTTP_HOST=0.0.0.0`.** `0.0.0.0` binds every
interface on the box — LAN, any public interface, and the tailnet IP all at
once — which throws away the isolation Tailscale is supposed to provide.
Bind to loopback (2a) or to the specific `100.x.y.z` tailnet address (2b),
never to the wildcard address.

## 3. Phone microphone / voice-upload: the HTTPS catch

The voice-upload receiver (`start-voice-upload.sh`) serves a small recorder
page that calls `navigator.mediaDevices.getUserMedia` to record from the
phone's microphone. Browsers only grant microphone access from a
[secure context](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts) —
in practice, HTTPS (or `localhost`). Plain `http://100.x.y.z:8767/` does
**not** qualify, even though it's already private to your tailnet. That gap
is exactly why a public `cloudflared` quick tunnel was used previously: it
was the easy way to get a real HTTPS URL in front of a browser.

Tailscale solves this without a public endpoint. With
[MagicDNS](https://tailscale.com/kb/1081/magicdns) enabled for your tailnet,
`tailscale serve` can terminate TLS using a real certificate issued by Let's
Encrypt for your node's `your-machine.your-tailnet.ts.net` name — trusted by
phone browsers, valid, and renewed automatically, but only resolvable and
reachable from inside your tailnet:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8767
```

(Again, treat this as illustrative — run `tailscale serve --help` to confirm
the flags for your Tailscale version.) Open
`https://your-machine.your-tailnet.ts.net:8443/` on the phone, enter the
upload token as usual, and the browser grants microphone access because the
context is secure — with no `trycloudflare.com` URL and nothing on the public
internet.

The phone needs the Tailscale app installed and logged into the same tailnet
as the host machine; otherwise it simply cannot resolve or route to
`*.ts.net` addresses.

## 4. Defense in depth: keep the bearer token

Tailscale and the MCP bearer token solve two different problems and neither
replaces the other:

- **Tailscale (network layer):** controls *who can reach the port at all* —
  only devices enrolled in your tailnet (and, per §5, only the ones your ACLs
  allow).
- **`STACKCHAN_MCP_AUTH_TOKEN` (application layer):** controls *what's
  allowed to call the API* once a connection reaches it.

The server fails closed: `mcp_server/server.py` refuses to start the HTTP
transport at all unless `STACKCHAN_MCP_AUTH_TOKEN` is set, and every request
without a matching `Authorization: Bearer <token>` header gets `401` (see the
fail-closed check added in PR #12). That behavior is unconditional — it
applies the same way whether the server is reachable over a public tunnel or
only over your tailnet, and it should stay on in both cases. A tailnet only
guarantees the *network path* is private; it says nothing about every device
or process that happens to be on that tailnet. A compromised or borrowed
tailnet node, a misbehaving local process on the same machine, or a phone
that gets lost while still enrolled are all still stopped by the bearer
check. Keep both layers.

## 5. Tighten tailnet ACLs

By default, every device in a tailnet can reach every other device on every
port ("everyone trusts everyone"). For a device that can physically move a
robot and speak through its speaker, narrow that down with
[Tailscale ACLs](https://tailscale.com/kb/1018/acls) so only the specific
nodes that need it — your agent host, your phone — can reach the MCP and
voice-upload ports on the Stack-chan host.

Tag the relevant nodes in the admin console, then restrict access by tag in
the tailnet policy file. Illustrative snippet (adapt tag names and ports to
your setup):

```json
{
  "tagOwners": {
    "tag:stackchan": ["autogroup:admin"],
    "tag:agent": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:agent", "autogroup:member"],
      "dst": ["tag:stackchan:8002", "tag:stackchan:8767"]
    }
  ]
}
```

This belongs in the tailnet's ACL policy file in the Tailscale admin console,
not in this repository. Treat real tag names, node names, and tailnet names
as deployment details, the same way you already treat `.env` values.

## 6. `cloudflared` is now optional

The public Cloudflare tunnel path (`STACKCHAN_ENABLE_PUBLIC_MCP_TUNNEL`,
`start-http.sh`'s `cloudflared tunnel run`, and the `cloudflared tunnel --url`
quick-tunnel flow for phone testing) remains supported for the case where a
device that genuinely cannot join your tailnet needs to reach the server. It
is no longer the recommended default.

If you do use it:

- Front it with authentication. The bearer-token check in §4 is mandatory,
  not optional, for anything reachable from the public internet; layering
  [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
  in front of the tunnel is a strong additional layer.
- Never reuse a hostname that has ever been exposed before. Treat any
  previously public tunnel hostname as permanently burned — it is already in
  CT logs and possibly in scraped history somewhere — and roll a freshly
  generated one if you stand the tunnel back up.

For normal day-to-day remote access, use Tailscale as described above
instead.
