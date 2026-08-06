# Changelog

Notable user-visible changes are recorded here. This project follows the
date-based tag format documented in `docs/branching-strategy.md` until a formal
firmware compatibility scheme is introduced.

## Unreleased

### Added

- Environmental sensing through the ENV III Unit and the `stackchan_sense` MCP
  tool.
- Compiled animated GIF expressions and a deterministic asset generator.
- Public contribution, support, conduct, security, and third-party attribution
  guidance.

### Changed

- Public examples no longer contain deployment-specific launchd identifiers or
  absolute user paths.
- AnimatedGIF is pinned exactly for reproducible PlatformIO resolution.

### Security

- Public MCP tunnel startup remains opt-in and requires bearer-token
  authentication.
- Non-loopback voice-upload deployments require an upload token.
