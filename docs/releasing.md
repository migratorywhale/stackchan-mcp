# Releasing Stack-chan MCP

Releases use annotated date-based tags such as `v2026.08.06`. Create releases
from a clean, up-to-date `master` branch only.

## Pre-release checklist

1. Confirm that repository code, vendored code, and face artwork can be
   distributed under the licenses recorded in `LICENSE` and
   `THIRD_PARTY_NOTICES.md`.
2. Move the relevant `CHANGELOG.md` entries from `Unreleased` into a dated
   release section.
3. Run `make ci-local`.
4. Run a dedicated secret scanner such as Gitleaks against all refs and the
   complete Git history, not only the checked-out tree.
5. Inspect `cd firmware && pio pkg list` and record resolved direct and
   transitive dependency versions in the release notes.
6. Verify the firmware on the supported CoreS3 hardware without consuming a
   pending recording accidentally. Use `/audio/status`, not `GET /audio`, for
   the non-destructive readiness check.
7. Create an annotated tag and push it:

   ```bash
   git tag -a vYYYY.MM.DD -m "release vYYYY.MM.DD"
   git push origin vYYYY.MM.DD
   ```

The release workflow rebuilds the firmware from source, runs the repository
quality gates, publishes the firmware binary and ELF, and includes SHA-256
checksums. Release artifacts do not include Wi-Fi credentials or local
`firmware/src/config.h` values.

## Compatibility notes

Every release note must call out changes to:

- Firmware HTTP endpoints or request/response formats.
- MCP tool names or behavior.
- Required environment variables.
- Flash filesystem or face-asset generation.
- Supported hardware and required peripheral wiring.
