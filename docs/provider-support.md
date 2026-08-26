# Provider support

| Provider | Status | Initial source |
| --- | --- | --- |
| Codex | Supported | Local rollout JSONL and optional metadata-only subagent state |
| Claude Code | Supported (core) | Local project transcript JSONL |
| OpenCode | Planned | To be verified before implementation |
| Kilo Code | Planned | To be verified before implementation |
| Pi | Planned | To be verified before implementation |

“Supported” means the adapter has synthetic fixtures, extracts conversations, turns,
models, token usage, and tool names when available, and passes privacy regression tests.
It does not mean that token counters are equivalent to invoices.

`--provider all` detects every supported provider from its expected data directory and
ingests each metadata-only snapshot independently. With no explicit source it checks
the local default homes; repeated `--source` paths are filtered by detected format.

Codex additionally exposes provider-reported turn duration and TTFT, model context
window samples, bounded reasoning/collaboration/service-tier labels, timestamped
compactions, technical work-item categories and durations, and local thread-spawn
relationships. Work-item content and rate-limit payloads are deliberately excluded.
Subagent state can remain `open` after a child thread is technically closed; reporting
therefore derives closure from collected child turns when they are available.

Claude Code reads top-level sessions from
`~/.claude/projects/<project>/<session-id>.jsonl` (or `CLAUDE_CONFIG_DIR`). It extracts
main-session turns, models, token usage, tool names, and compaction timestamps while
discarding prompts, responses, tool inputs/results, paths, branches, and arbitrary
metadata. Streaming fragments are deduplicated by request or message identifier.

Claude Code emits uncached, cache-read, and cache-creation input separately. Normalized
`input_tokens` is their sum, with each component retained in its corresponding field.
The internal transcript schema can change between Claude Code releases and local usage
is not billing data. This first increment does not collect subagent transcripts,
context-window sizes, effort/service-tier settings, TTFT, provider-reported duration,
or technical work-item intervals.

Provider formats can change without notice. Unknown fields are ignored; malformed JSONL
records are counted and skipped. Compatibility fixes should add a fixture for both the
old and new format whenever possible.
