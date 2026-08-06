# Support

Thanks for using `stackchan-mcp`.

## Support Boundaries

This project is maintained on a best-effort basis. Maintainers may not be able
to provide custom debugging, hardware bring-up, or environment-specific support
for every setup.

## Where To Ask For Help

- Use GitHub Discussions if the repository has them enabled.
- Otherwise, open a GitHub issue with the closest issue template.
- For security issues, do not open a public issue. Follow `SECURITY.md`
  instead.

## Before Opening An Issue

- Check `README.md`, `CONTRIBUTING.md`, and existing issues first.
- Reduce the problem to the smallest reproducible case you can share.
- Say whether the problem is in firmware, the MCP server, hardware wiring, or
  your local environment.

## Safety And Privacy

- Redact API keys, Wi-Fi credentials, public tunnel hostnames, and private LAN
  IP addresses before posting logs or screenshots.
- Do not attach raw voice recordings, transcripts, or camera snapshots unless
  they are necessary for debugging and safe to share publicly.
- `GET /audio` consumes and clears the pending recording buffer on the device.
  Prefer `GET /audio/status` for non-destructive checks.

## What To Include

- Exact commit, branch, or release if known
- Device model and relevant hardware add-ons
- Reproduction steps
- Expected behavior and actual behavior
- Relevant logs, with secrets and private network details removed
- Whether a public MCP tunnel was enabled
