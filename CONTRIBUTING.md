# Contributing

Thanks for helping improve `stackchan-mcp`. This project touches both host-side
Python tooling and firmware that runs on a physical M5Stack CoreS3, so small,
well-tested changes are easiest to review.

By submitting a contribution, you certify that you created it or otherwise
have the right to submit it under this repository's MIT License. Record the
source and license of third-party code, generated assets, and artwork in
`THIRD_PARTY_NOTICES.md`; do not copy material whose redistribution terms are
unknown.

## Before You Open A Pull Request

Run the smallest useful check while iterating, then run the broader gate before
handoff.

| Change type | Minimum check | Broader handoff check |
| --- | --- | --- |
| Python or MCP server only | `uv run ruff check .`, `uv run pyright`, and `uv run pytest` | `make lint` |
| MCP tool behavior or guardrails | `make test-mcp` | `make lint` and `make test` |
| Firmware only | `cd firmware && pio run -e m5stack-cores3` | `make lint` and `make test` |
| Firmware parser or utility code | `cd firmware && pio test -e native` | `cd firmware && pio run -e m5stack-cores3` |
| HTTP contract shared by firmware and MCP | `uv run pytest` and `cd firmware && pio run -e m5stack-cores3` | `make lint` and `make test` |
| Face assets under `firmware/data/` | `cd firmware && pio run -t uploadfs` before device use | Document filename/path changes |

The GitHub Actions workflow runs the host Python checks plus firmware native
tests, CoreS3 build, and high-severity `pio check`.

Python type checking uses `pyright` in basic mode for `mcp_server/` and
`scripts/`. Tests intentionally stay outside the typecheck gate so local fake
clients can remain lightweight.

Python tests report coverage for `mcp_server/` and `scripts/`. Coverage is
reported as visibility, not as a hard threshold, until the important host-side
paths have a stronger baseline.

Ruff formatting is available with `uv run ruff format .`, but formatter checks
are not a CI gate until the existing baseline is normalized. If you run the
formatter, make that a style-only PR.

## Local Setup

```sh
uv sync --dev
python -m pip install platformio==6.1.18
```

Firmware configuration is intentionally local:

```sh
cd firmware
cp config.h.example src/config.h
```

Edit `firmware/src/config.h` with your Wi-Fi and host settings. Do not commit
the edited file.

For host-side secrets and local network details:

```sh
cp .env.example .env
```

Do not commit `.env`, API keys, voice model ids, upload tokens, public tunnel
hostnames, or private LAN addresses.

## Git Hooks

This repository uses local Git hooks for fast checks and commit-message
validation. Enable them after cloning:

```sh
make install-hooks
```

By default it runs `uv run ruff check .` and `uv run pytest
tests/test_mcp_server.py`. To run the full Makefile gate before a commit:

```sh
STACKCHAN_HOOK_FULL=1 git commit
```

The same hook path enables a `commit-msg` hook that rejects commit subjects
which do not follow Conventional Commits. This is required for local commits.

## Commit Messages

Use Conventional Commits for every commit:

```text
<type>(<scope>): <description>
```

Use `feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, `chore`,
`perf`, or `style` as the type. Add a scope when it clarifies the affected
area, such as `firmware`, `mcp`, `docs`, `tests`, `ci`, or `deps`.

Examples:

```text
fix(firmware): handle queued wav playback safely
ci(python): add pyright gate
docs: document live-device audio checks
```

For breaking changes, add `!` after the type or scope and include a
`BREAKING CHANGE:` footer.

## Branching

Use short-lived topic branches and merge through protected `master`. The
recommended branch prefixes are `feat/`, `fix/`, `ci/`, `docs/`, `chore/`,
`deps/`, and temporary `experiment/` branches for hardware or protocol
exploration.

See `docs/branching-strategy.md` for the full policy.

## Dependency Policy

This is a public OSS repository, so dependency changes should be conservative
and reviewable.

- Pin direct Python dependencies exactly in `pyproject.toml`; `uv.lock` remains
  the source of truth for the full resolved graph.
- Pin direct PlatformIO platforms and libraries exactly in
  `firmware/platformio.ini`. Do not use unbounded, caret, wildcard, branch, or
  `latest` dependency specs.
- PlatformIO can still resolve nested library dependencies from direct
  dependencies. Check `cd firmware && pio pkg list` in every PlatformIO
  dependency PR and call out any nested dependency that moved.
- Pin GitHub Actions to commit SHAs. Keep a comment with the human-readable tag
  next to the SHA.
- Use Dependabot PRs for routine Python and GitHub Actions updates. The
  configured cooldown is 7 days for patch, 14 days for minor, and 30 days for
  major Python updates; GitHub Actions wait 14 days.
- PlatformIO updates are manual because Dependabot does not manage this
  manifest here. Wait at least 14 days after release for libraries and 30 days
  for platforms/toolchains unless fixing a confirmed vulnerability or hardware
  blocker.
- Security updates may bypass the cooldown only when the PR or commit explains
  the advisory, affected versions, and verification performed.
- Every dependency update must run the narrow relevant checks and the broader
  handoff gate before merge.

## Hardware And Safety Notes

- The firmware HTTP API is designed for a trusted LAN. Do not expose the CoreS3
  device directly to the internet without adding an authentication layer.
- `GET /audio` consumes and clears the pending recording buffer. Prefer
  `/audio/status` for non-destructive checks.
- Keep MCP tests isolated from live devices. Unit tests should mock HTTP
  clients rather than calling the robot.
- If you change an HTTP endpoint, update the firmware, Python caller, tests,
  and docs together.

## Observability

For non-destructive probes, logs, metric fields, and deployment-local alert
candidates, see `docs/observability.md`.

## Documentation

For non-trivial debugging sessions, add a note under `docs/` using:

```text
docs/<topic>-troubleshooting-YYYY-MM-DD.md
```

Include symptoms, investigation steps, root cause or current best hypothesis,
final fix, and concrete verification commands/results.
