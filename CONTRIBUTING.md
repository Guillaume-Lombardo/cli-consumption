# Contributing

## Set up the project

```bash
git switch main
git pull --ff-only
git switch -c feat/short-description
uv sync --all-extras --all-groups
uv run pre-commit install
```

Use `uv add`, `uv remove`, and `uv lock` for dependency changes. Do not edit the lock
file manually.

## Make a change

Keep provider parsing behind the adapter interface and normalized persistence behind
the storage module. Register providers once in the adapter registry rather than adding
parallel CLI or detection lists. A new field must have a documented meaning across
providers or be explicitly namespaced as provider-specific.

Do not add telemetry. Test fixtures must be synthetic and must not contain copied user
conversations or credentials.

Schema changes require a forward Alembic migration for both SQLite and PostgreSQL, an
explicit downgrade boundary, legacy-adoption tests, and mixed-version deployment notes.
Snapshot protocol changes must preserve strict validation, bounded payloads, generic
errors, and the documented server-first upgrade sequence. Export changes must preserve
deterministic ordering, bounded-memory CSV streaming, complete selected conversation
graphs, and spreadsheet-formula neutralization.

Provider readers must use the bounded file and JSONL helpers in `adapters/_shared.py`,
must not follow direct provider-file symlinks, and must stay within the shared snapshot
record budget. Every registered adapter test module must include a synthetic canary and
assert its absence from the normalized snapshot; shared privacy tests cover all later
output surfaces.

Reuse adapter primitives from `adapters/_shared.py` only when their malformed-input,
boundary, and normalization semantics are identical. Keep provider-specific timestamp
thresholds, token composition, ranking, project inference, and other compatibility
heuristics local even when their implementations look similar.

## Validate and review

Run the quality gates documented in `AGENTS.md`, inspect the complete diff, then open a
pull request. The pull request must explain the behavior, privacy impact, storage
compatibility, tests, and any remaining limitation. Merge by squash after required
checks pass and review is complete.

## Release

The source distribution is deliberately limited to the project metadata, README,
license notices, and complete `src/cli_consumption` package. It excludes tests and
repository-only automation; Hatchling also carries `.gitignore` so downstream builds
continue to exclude local artifacts. `tests/test_packaging.py` builds the sdist,
reconstructs a wheel from it, and verifies both artifact manifests; update that
contract when adding new runtime package data.

Update the project version with `uv version <version>` and include the resulting
`pyproject.toml` and `uv.lock` changes in a pull request. When that version change is
squash-merged into `main`, the release workflow reruns the quality gates, builds the
distributions, tags the merge commit as `v<version>`, and publishes to PyPI through
Trusted Publishing. Do not create the release tag manually.

If a release fails before publication, rerun it for the original version commit with
`gh workflow run release.yaml --ref main -f release_sha=<merge-commit>`. The workflow
rejects commits outside `main` and conflicting existing tags.
