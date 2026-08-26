# Provider support

| Provider | Status | Initial source |
| --- | --- | --- |
| Aider | Supported (core) | Opt-in local analytics JSONL |
| Codex | Supported | Local rollout JSONL and optional metadata-only subagent state |
| Claude Code | Supported (core) | Local project transcript JSONL |
| Gemini CLI | Supported (core) | Local automatic chat history JSON and JSONL |
| Goose | Supported (core) | Local SQLite session store v16 |
| Kilo Code | Supported (core) | Local SQLite session store |
| OpenCode | Supported (core) | Local SQLite v2 session store |
| Pi | Supported (core) | Local session JSONL v1-v3 |
| Qwen Code | Supported (core) | Local append-only transcript JSONL |

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

Gemini CLI reads automatic session history from
`~/.gemini/tmp/<project-hash>/chats/session-*.jsonl` and the legacy `.json` form. It
replays append-only message updates, metadata checkpoints, and rewinds before extracting
user turns, Gemini model calls, token usage, and tool names. Prompts, responses, thoughts,
summaries, memory scratchpads, tool arguments/results/status details, directories, and
project hashes are discarded.

Gemini reports prompt, cached prompt, visible candidate, thought, tool-prompt, and total
token counters. Normalized input is the prompt count, cached input is its bounded subset,
and normalized output combines candidate and thought tokens. Provider totals above the
attributed input/output sum remain unattributed; tool-prompt tokens are treated as a
prompt subset rather than added twice. This adapter does not derive a project name from
Gemini's one-way project hash, collect nested subagent sessions, compaction timestamps,
context-window sizes, configuration labels, tool outcomes, or provider-reported
durations. Gemini CLI's internal history schema can change without notice, and local
token events are not billing data.

Goose reads `sessions.db` from its sessions directory (normally
`~/.local/share/goose/sessions/`). The adapter was qualified against Goose v1.47.0
and schema v16. It extracts visible user turns, per-request model and token usage,
tool names, and compaction markers while discarding session names, prompts, responses,
thinking, tool arguments/results, paths, recipes, arbitrary metadata, errors, costs,
and cost-source labels. Working directories are inspected only for explicit project
mappings.

Goose reports input tokens with cache reads and writes as subsets. Normalized uncached
input subtracts both cache subsets, and model labels combine the session provider with
the usage-ledger model. Usage-ledger timestamps have one-second resolution, so model
calls are attributed to the latest visible user turn at or before the ledger event.
This adapter does not read legacy pre-1.10 JSONL sessions or pre-v16 SQLite schemas,
collect parent/subagent relationships, context-window sizes, reasoning tokens,
provider-reported latency, or cost. Goose's internal schema can change without notice,
and local token events are not billing data.

OpenCode reads `opencode.db` from its XDG data directory (normally
`~/.local/share/opencode/`). It extracts v2 session messages, model references, token
usage, tool names, and compaction timestamps while discarding message text, reasoning,
tool inputs/results, shell commands/output, paths, titles, errors, costs, and arbitrary
metadata. Model labels combine OpenCode's provider and model identifiers.

OpenCode reports uncached input, cache reads, cache writes, visible output, and
reasoning separately. Normalized input and output totals include their respective
components. The adapter does not currently read pre-v2 JSON storage, legacy
`message`/`part` tables, child-session relationships, context-window sizes, or
provider-reported cost. The SQLite schema is internal and may change without notice;
local token events are not billing data.

Kilo Code reads `kilo.db` from its data directory (normally
`~/.local/share/kilo/`). The adapter was qualified against Kilo Code CLI v7.5.5 and
the matching current `session`, `message`, and `part` schema. It extracts user turns,
assistant model calls, model references, token usage, tool names, and compaction parts
while discarding titles, prompts, responses, reasoning, tool inputs/results, commands,
errors, paths, diffs, costs, snapshots, and arbitrary metadata.

Kilo Code reports uncached input, cache reads, cache writes, visible output, and
reasoning separately. Normalized input and output totals include their respective
components. The adapter does not read the legacy IDE extension's task files, cloud-only
sessions, `session_message` queue records, child-session relationships, context-window
sizes, costs, snapshots, or provider-reported generation metrics. `KILO_DB` can select
a non-default database, which must be passed explicitly with `--source`. Kilo Code's
SQLite schema can change without notice, and its local token events are not billing
data.

Pi reads session JSONL files under `~/.pi/agent/sessions/` (or copied agent
directories). It extracts user turns, assistant and compaction model calls, provider
and model identifiers, token usage, tool names, thinking-level changes, and compaction
timestamps while discarding prompts, responses, thinking, tool arguments/results,
commands/output, summaries, errors, costs, paths, extension data, and arbitrary
metadata. All persisted branches are counted because Pi retains branch history in the
same session file.

Pi reports uncached input, cache reads, cache writes, output, and optional reasoning
tokens. Normalized input includes all three input components; reasoning is retained as
a subset of output rather than added twice. This adapter does not collect custom
session directories automatically, context-window sizes, costs, branch relationships,
or provider-reported durations. Pi's JSONL schema can change without notice, and its
local token events are not billing data.

Qwen Code reads active session transcripts from
`~/.qwen/projects/<project-id>/chats/<session-id>.jsonl` (or `QWEN_HOME`). It
follows the latest `uuid`/`parentUuid` branch so turns abandoned by rewind are not
counted, and extracts user turns, assistant model calls, token usage, context-window
sizes, function-call names, and chat-compression timestamps. Prompts, responses,
thoughts, tool arguments/results, working directories, branches, titles, errors,
hooks, checkpoints, arbitrary system payloads, archived sessions, and sidechain agent
records are discarded. Working directories are inspected only for explicit project
mappings.

Qwen Code persists prompt, cached-prompt, visible-candidate, thought, tool-prompt,
and total token counters in Gemini-compatible usage metadata. Normalized input is the
prompt total, cached input is its bounded subset, and normalized output combines
candidate and thought tokens. Provider totals above the attributed input/output sum
remain unattributed; tool-prompt tokens are treated as a prompt subset. Cache-creation
input is unavailable in the serialized metadata. This adapter was qualified against
Qwen Code v0.22.2. Its internal transcript schema can change without notice, and local
token events are not billing data.

Aider reads an explicitly configured local analytics log named `analytics.jsonl` (for
example, `AIDER_ANALYTICS_LOG=~/.aider/analytics.jsonl`). It groups events from
`launched` through `exit`, extracts message-send attempts, model identifiers, and
prompt/completion/total token counters, and discards the analytics user UUID, costs,
exception text, command events, edit formats, system properties, and arbitrary event
properties. Conversation identifiers are stable hashes of the transient UUID and
launch time; the UUID itself is never emitted.

Aider analytics do not expose prompts or responses, but logging is not enabled at a
fixed location by default. The adapter cannot attribute projects, tool calls, cached
or reasoning tokens, compactions, context windows, or provider-reported durations.
Turns represent Aider message-send attempts because the log has no durable user-turn
identifier. Unknown custom model names may already be represented as
`provider/REDACTED` by Aider. Its analytics event schema can change without notice,
and local token events are not billing data.

Provider formats can change without notice. Unknown fields are ignored; malformed
provider records are counted and skipped. Compatibility fixes should add a fixture for
both the old and new format whenever possible.
