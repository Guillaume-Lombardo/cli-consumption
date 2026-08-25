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

Codex additionally exposes provider-reported turn duration and TTFT, model context
window samples, bounded reasoning/collaboration/service-tier labels, timestamped
compactions, technical work-item categories and durations, and local thread-spawn
relationships. Work-item content and rate-limit payloads are deliberately excluded.
Subagent state can remain `open` after a child thread is technically closed; reporting
therefore derives closure from collected child turns when they are available.

Provider formats can change without notice. Unknown fields are ignored; malformed JSONL
records are counted and skipped. Compatibility fixes should add a fixture for both the
old and new format whenever possible.
