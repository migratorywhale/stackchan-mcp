# Universal lock dependency snapshot failure

## Symptom and reproduction

The Security workflow at `ca393b1` passed its repository and dependency audits,
but failed in `dependency-submission` before uploading the snapshot:

```text
ValueError: multiple locked distributions normalize to 'numpy'
```

Running `scripts/build_dependency_snapshot.py` against the committed `uv.lock`
reproduced the failure without installing the optional face-tracking dependency
or contacting a device. This was not a vulnerability finding or a firmware fault.

## Cause

`uv.lock` is a universal lock. OpenCV references NumPy 2.4.6 for Python below
3.12 and NumPy 2.5.2 for Python 3.12 and newer. These are alternative resolution
branches, not two simultaneously installed NumPy distributions.

The exporter indexed distributions, traversal state, and edges by normalized
name alone. Its duplicate guard rejected this valid lock. Removing the guard
alone would instead overwrite a version and produce an incomplete inventory.

## Fix

- Key registry packages and traversal state by normalized name and version.
- Resolve edges using their locked version and source; name-only edges must
  have exactly one candidate. Reject ambiguity, missing pinned versions, and
  duplicate name/version identities rather than selecting arbitrarily.
- Keep marker branches in the universal inventory without evaluating them
  against the Python version running the exporter.
- Track activated extras, runtime/development scope, and direct/transitive
  relationships separately for each version.
- Retain the PyPI-only inventory boundary and bump the detector version to 2.

No dependency pins, workflow permissions, running services, or firmware change.
Rollback is a revert of this isolated script/test/documentation change; it would
restore the old export failure until another fix is applied.

## Verification

- Added focused regressions for version-qualified edges, marker branches,
  scope and extras isolation, cycles, normalization collisions, unreferenced
  versions, source validation, deterministic lock ordering, and the real lock.
- `uv run --locked pytest -q`: 144 passed (one existing Starlette deprecation
  warning).
- `uv run --locked ruff check .`: passed.
- `uv run --locked pyright`: zero errors.
- The CLI on Python 3.11 successfully emitted 84 registry distributions,
  including both NumPy versions, with complete dependency edges.

The real-lock regression also runs in pull-request CI so future lock updates
exercise the exporter before default-branch dependency submission.
