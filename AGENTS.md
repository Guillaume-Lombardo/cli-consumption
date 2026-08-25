# Repository instructions

## Authority and scope

Read this file, `README.md`, `CONTRIBUTING.md`, and the relevant documents under
`docs/` before changing the repository. Keep changes scoped to one short-lived branch
and one coherent pull request.

The project is a Python 3.14+ package managed exclusively with `uv`. Do not introduce
another package manager, task runner, ORM, web framework, or migration tool without an
accepted architecture decision.

## Product invariants

- Never collect or persist prompts, responses, tool arguments, environment values,
  credentials, or raw rollout events.
- Treat every provider file and API payload as untrusted input.
- Keep the normalized model provider-neutral; provider-specific parsing belongs in
  `src/cli_consumption/adapters/`.
- Support SQLite and PostgreSQL for every persisted schema change.
- Make ingestion idempotent and safe to repeat after partial failures.
- Preserve stable conversation IDs and prefer the most complete duplicate copy.
- Keep generated dashboards self-contained and free of network requests.
- Do not claim that local token events represent billing data.

## Required skills

- Use `.agents/skills/add-cli-adapter` for a new provider or provider format change.
- Use `.agents/skills/evolve-storage-schema` for tables, columns, indexes, migrations,
  retention, or compatibility changes.
- Use `.agents/skills/audit-usage-privacy` for collectors, API payloads, exports,
  dashboards, logging, or changes that can cross the privacy boundary.
- Use `.agents/skills/yeet-github` only when explicitly asked to publish a draft pull
  request. Use `.agents/skills/yolo` only when explicitly invoked for the full publish,
  squash-merge, and cleanup workflow.

## Quality gates

Run all of the following before requesting review:

```bash
uv run pre-commit run --all-files
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov --cov-report=term-missing
uv build
```

Add tests for every behavioral change. Include malformed and adversarial provider data
for parsers, idempotency cases for ingestion, authentication cases for the API, and
privacy assertions for every new exported field.

## Trunk-based workflow

- Branch from current `main` using a short-lived `feat/`, `fix/`, `docs/`, or `chore/`
  branch.
- Never push directly to `main`, force-push shared history, or bypass required checks.
- Prefer one focused commit before review and squash-merge the pull request.
- Rebase or merge current `main` before final validation when the branch is stale.
- Delete the source branch only after the pull request is verified as merged.

## Local orchestration files

`.agents/orchestrator.md` and `.agents/local-environment.md` are machine-specific and
ignored deliberately. Keep durable, portable decisions in tracked documentation or
architecture decision records instead.
