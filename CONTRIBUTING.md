# Contributing

## Set up the project

```bash
git switch main
git pull --ff-only
git switch -c feat/short-description
uv sync --all-groups
uv run pre-commit install
```

Use `uv add`, `uv remove`, and `uv lock` for dependency changes. Do not edit the lock
file manually.

## Make a change

Keep provider parsing behind the adapter interface and normalized persistence behind
the storage module. A new field must have a documented meaning across providers or be
explicitly namespaced as provider-specific.

Do not add telemetry. Test fixtures must be synthetic and must not contain copied user
conversations or credentials.

## Validate and review

Run the quality gates documented in `AGENTS.md`, inspect the complete diff, then open a
pull request. The pull request must explain the behavior, privacy impact, storage
compatibility, tests, and any remaining limitation. Merge by squash after required
checks pass and review is complete.
