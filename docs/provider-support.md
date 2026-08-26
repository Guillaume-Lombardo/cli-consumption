# Provider support

| Provider | Status | Initial source |
| --- | --- | --- |
| Aider | Supported (core) | Opt-in local analytics JSONL |
| Amp | Supported (core) | Local thread mirror JSON |
| Codex | Supported | Local rollout JSONL and optional metadata-only subagent state |
| Crush | Supported (core) | Per-project local SQLite store |
| Cursor CLI | Supported (core) | Local Composer 2 transcript JSONL and chat metadata SQLite |
| Claude Code | Supported (core) | Local project transcript JSONL |
| Gemini CLI | Supported (core) | Local automatic chat history JSON and JSONL |
| GitHub Copilot CLI | Supported (core) | Local session event JSONL |
| Goose | Supported (core) | Local SQLite session store v16 |
| Grok Build | Supported (core) | Local session summary and update/event JSONL |
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

Cursor CLI reads Composer 2 transcripts from
`~/.cursor/projects/*/agent-transcripts/<session-id>/<session-id>.jsonl` and optional
session metadata from `~/.cursor/chats/*/<session-id>/store.db`. It extracts visible
user turns, assistant-response counts, bounded tool names, session creation time, and
the latest selected model label while discarding prompts, responses, thinking, tool
arguments/results, titles, modes, credentials, working directories, raw message blobs,
and arbitrary metadata. Project paths are matched only against explicit mappings and
are never reconstructed or persisted from Cursor's encoded directory names. The
adapter was qualified against the current Cursor CLI Composer 2 format in August 2026.

Cursor transcripts do not include per-message timestamps, token usage, or model
identifiers. Assistant records are therefore represented as model calls with an
`unknown` model and zero tokens; the session's `lastUsedModel` is retained only in the
conversation model list and is not attributed historically to those calls. File
modification time is used only as the approximate conversation end. Database-only
sessions expose creation time and the latest model but no turns. Legacy text
transcripts, Cursor IDE history, background/cloud agents, subagent transcripts,
compactions, context windows, costs, and provider-reported durations are not collected.
The internal formats can change without notice, and local events are not billing data.

Crush reads its global project registry at
`~/.local/share/crush/projects.json` and each registered project's `crush.db`, or a
direct copied project/data directory. The adapter was qualified against Crush v0.91.2
and its current additive SQLite migrations. It extracts top-level sessions, user
turns, assistant model/provider labels, tool names, finish status, summary compactions,
and the session-level token snapshot while discarding titles, prompts, responses,
reasoning, tool inputs/results, shell commands/output, paths, todos, costs, and
arbitrary part data. Project paths are inspected only for explicit project mappings.

Crush does not persist per-call token usage. Its session counters represent the latest
context footprint rather than additive conversation usage, so the adapter attributes
that single snapshot to the last assistant model call and does not claim it as billing
data. Cache and reasoning splits are unavailable. Child agent sessions, costs,
attachments, provider-reported latency, and context-window sizes are not collected.
The SQLite schema is internal and can change without notice.

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

Grok Build reads session directories under
`~/.grok/sessions/<encoded-cwd>/<session-id>/`. It extracts stable session and
prompt identifiers, timestamps, terminal turn status, model labels, per-turn
and per-model token aggregates, reasoning effort, time to first token, tool
names, and successful auto-compaction markers from `summary.json`,
`updates.jsonl`, and `events.jsonl`. Working directories are inspected only for
explicit project mappings. Titles, prompts, responses, agent results, tool
arguments/results, errors, Git metadata, costs, billing data, arbitrary update
metadata, summaries, compaction content, and raw events are discarded.

Grok Build's `TurnCompleted.usage` is a per-prompt aggregate. A normalized
model-call row therefore represents one model aggregate within a prompt, while
the turn retains the provider-reported model-call count. Full input includes
cache-read and cache-creation subsets; normalized uncached input subtracts both,
and reasoning is retained as an output subset. Sessions written before durable
turn completions may expose turn timing, models, and tools but no token usage.
Rewinds, subagent relationships, costs, context-window samples, tool outcomes,
and manual compactions without a successful auto-compaction update are not
collected. The adapter was qualified against the open-source Grok Build session
schema in August 2026. Local usage events are not billing records, and the
internal format can change without notice.

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

GitHub Copilot CLI reads session event logs from
`~/.copilot/session-state/<session-id>/events.jsonl`. The adapter was qualified
against GitHub Copilot CLI v1.0.80 and session event schema v1. It extracts root
user turns, assistant model labels, tool names, successful compaction timestamps,
and the latest per-model token aggregates written at session shutdown. Prompts,
responses, reasoning, tool arguments/results, errors, paths, repository metadata,
request identifiers, costs, quotas, code-change metrics, subagent events, and
arbitrary event data are discarded. Working directories are inspected only for
explicit project mappings.

Per-call `assistant.usage` events are ephemeral and are not written to the local
event log. Consequently, each latest shutdown aggregate is represented as one
unattributed model snapshot per model; token usage and model-call counts cannot be
assigned to individual turns. `inputTokens` includes cache reads and writes, which
are retained as subsets and subtracted to derive uncached input. In-progress sessions
without a shutdown event expose turns, models, and tools but no token totals. The
adapter does not read legacy `history-session-state`, workspace artifacts, synced
cloud sessions, subagent relationships, context-window samples, cost, or billing
data. The local session schema can change without notice, and local token aggregates
are not billing records.

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

Amp reads thread JSON files under `~/.local/share/amp/threads/`. It extracts visible
user turns, assistant model calls, per-inference token usage, tool names, and context
window samples while discarding titles, prompts, responses, thinking, tool
arguments/results, environment details, repository paths, traces, errors, credits,
costs, and arbitrary metadata. Working directories are inspected only for explicit
project mappings.

Amp splits input across uncached, cache-read, and cache-creation counters. For
`gpt-*` models, Amp uses the cache-creation counter for uncached prompt tokens, so the
adapter folds that bucket into uncached input; other model families retain Amp's
reported cache-creation split. Historical `usageLedger.events` take precedence over
duplicated message usage, while current files use `messages[].usage`. Context-window
samples are collected only when `maxInputTokens` is present. The adapter does not
collect thread content, subthread relationships, compaction markers, reasoning-token
splits, costs, credits, or provider-reported latency. Amp's local mirror format is
internal and can change without notice, and local token events are not billing data.

Provider formats can change without notice. Unknown fields are ignored; malformed
provider records are counted and skipped. Compatibility fixes should add a fixture for
both the old and new format whenever possible.
