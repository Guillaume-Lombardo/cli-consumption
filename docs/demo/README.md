# Synthetic dashboard demo

Every label, timestamp, counter, relationship, model name, and identifier in this
directory is an invented fixture defined in `generate.py`. The generator constructs
provider-neutral snapshots in memory, validates and ingests them into a temporary
SQLite database, normalizes ingestion timestamps and identifiers for reproducibility,
and removes the database automatically.

Regenerate and verify the self-contained dashboard:

```bash
uv run python docs/demo/generate.py
uv run pytest tests/test_demo.py
```

`dashboard.png` is a 1440×900 light-mode Chromium capture of `dashboard.html`, not an
independent mockup. It can be refreshed on a workstation with Playwright and Chromium
runtime dependencies installed:

```bash
npx --yes playwright@1.62.1 screenshot \
  --viewport-size="1440,900" --color-scheme=light \
  --wait-for-selector="#conversationCount" --wait-for-timeout=500 \
  "file://$PWD/docs/demo/dashboard.html" docs/demo/dashboard.png
```

The Python build contract excludes the whole `docs/` tree, tests, and repository-only
assets from both wheels and source distributions.
