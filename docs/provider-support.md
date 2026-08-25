# Provider support

| Provider | Status | Initial source |
| --- | --- | --- |
| Codex | Supported | Local rollout JSONL and optional metadata-only subagent state |
| Claude Code | Planned | To be verified before implementation |
| OpenCode | Planned | To be verified before implementation |
| Kilo Code | Planned | To be verified before implementation |
| Pi | Planned | To be verified before implementation |

“Supported” means the adapter has synthetic fixtures, extracts conversations, turns,
models, token usage, and tool names when available, and passes privacy regression tests.
It does not mean that token counters are equivalent to invoices.

Provider formats can change without notice. Unknown fields are ignored; malformed JSONL
records are counted and skipped. Compatibility fixes should add a fixture for both the
old and new format whenever possible.
