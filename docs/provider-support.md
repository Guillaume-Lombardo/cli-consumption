# Provider support

| Provider | Status | Initial source |
| --- | --- | --- |
| Aider | Supported (core) | Opt-in local analytics JSONL |
| Amazon Q Developer CLI | Supported (core) | Local persistent-conversation SQLite store |
| Amp | Supported (core) | Local thread mirror JSON |
| Cline CLI | Supported (core) | Local session SQLite index and message JSON |
| Codex | Supported | Local rollout JSONL and optional metadata-only subagent state |
| Continue CLI | Supported (core) | Local session JSON |
| Crush | Supported (core) | Per-project local SQLite store |
| Cursor CLI | Supported (core) | Local Composer 2 transcript JSONL and chat metadata SQLite |
| Claude Code | Supported (core) | Local project transcript JSONL |
| Gemini CLI | Supported (core) | Local automatic chat history JSON and JSONL |
| GitHub Copilot CLI | Supported (core) | Local session event JSONL |
| Goose | Supported (core) | Local SQLite session store v16 |
| Grok Build | Supported (core) | Local session summary and update/event JSONL |
| Kilo Code | Supported (core) | Local SQLite session store |
| Kimi Code CLI | Supported (core) | Local Wire event JSONL |
| OpenCode | Supported (core) | Local SQLite v2 session store |
| OpenHands CLI | Supported (core) | Local SDK conversation state and event JSON |
| Pi | Supported (core) | Local session JSONL v1-v3 |
| Plandex | Supported (core) | Copied self-hosted server conversation JSON |
| Qwen Code | Supported (core) | Local append-only transcript JSONL |

“Supported” means the adapter has synthetic fixtures, extracts conversations, turns,
models, token usage, and tool names when available, and passes privacy regression tests.
It does not mean that token counters are equivalent to invoices.

`--provider all` detects every supported provider from its expected data directory and
ingests each metadata-only snapshot independently. With no explicit source it checks
the local default homes; repeated `--source` paths are filtered by detected format.

## Codex

Codex additionally exposes provider-reported turn duration and TTFT, model context
window samples, bounded reasoning/collaboration/service-tier labels, timestamped
compactions, technical work-item categories and durations, and local thread-spawn
relationships. Work-item content and rate-limit payloads are deliberately excluded.
Subagent state can remain `open` after a child thread is technically closed; reporting
therefore derives closure from collected child turns when they are available.

## Cursor CLI

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

## Continue CLI

Continue CLI reads session JSON files from `~/.continue/sessions/`. A custom
`CONTINUE_GLOBAL_DIR` must be passed with `--source`. The adapter was qualified against
the current session format used by Continue CLI in August 2026. It extracts visible
user turns, assistant model labels, per-response token usage when present, the
cumulative session token snapshot, and function-call names while discarding titles,
prompts, responses, reasoning, tool arguments/results, context items, editor state,
rules, arbitrary metadata, costs, and credentials. Working directories are inspected
only for explicit project mappings.

Continue reports prompt and completion totals with optional cache-read, cache-write,
and reasoning subsets. The adapter prefers per-response usage and adds only the
unattributed remainder of the cumulative session snapshot, preventing double counting.
Session files do not persist reliable per-message timestamps, so model calls and turns
have no timestamps or durations; the file modification time is retained only as the
conversation's approximate end time. The adapter does not collect IDE-extension-only
history stores, context-window sizes, compactions, provider-reported status, latency,
or cost. Continue's internal session format can change without notice, and local token
events are not billing data.

## Crush

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

## Claude Code

Claude Code reads top-level sessions from
`~/.claude/projects/<project>/<session-id>.jsonl`. A custom `CLAUDE_CONFIG_DIR` must
be passed with `--source`. It extracts
main-session turns, models, token usage, tool names, and compaction timestamps while
discarding prompts, responses, tool inputs/results, paths, branches, and arbitrary
metadata. Streaming fragments are deduplicated by request or message identifier.

Claude Code emits uncached, cache-read, and cache-creation input separately. Normalized
`input_tokens` is their sum, with each component retained in its corresponding field.
The internal transcript schema can change between Claude Code releases and local usage
is not billing data. This first increment does not collect subagent transcripts,
context-window sizes, effort/service-tier settings, TTFT, provider-reported duration,
or technical work-item intervals.

## Gemini CLI

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

## Goose

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

## Grok Build

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

## OpenCode

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

## OpenHands CLI

OpenHands CLI reads SDK conversation persistence from
`~/.openhands/conversations/<conversation-id>/`, including `base_state.json` and
individual event JSON files. The adapter was qualified against OpenHands CLI v1.16.0
and the compatible current SDK persistence format in August 2026. It extracts user
turns, per-request model and token usage, tool names, context-window sizes, bounded
reasoning-effort labels, and condensation timestamps. Working directories are
inspected only for explicit project mappings. Prompts, responses, thoughts, tool
arguments/results, commands/output, errors, summaries, agent settings, hooks, tags,
credentials, costs, paths, arbitrary state, and raw events are discarded.

OpenHands normally reports cache reads and writes as subsets of prompt tokens; when
their sum exceeds the prompt counter, the adapter follows the SDK's compatibility
semantics and treats those cache counters as separate input buckets. All persisted
event branches are counted because they represent model consumption already incurred.
Older aggregate-only metrics are retained as unattributed model snapshots. The
adapter does not collect cloud-only conversations, delegate relationships, cost,
critic data, tool outcomes, or provider-reported response latency. OpenHands SDK
persistence can change without notice, and local token events are not billing data.

## Kilo Code

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

## Pi

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

## GitHub Copilot CLI

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

## Qwen Code

Qwen Code reads active session transcripts from
`~/.qwen/projects/<project-id>/chats/<session-id>.jsonl`. A custom `QWEN_HOME` must
be passed with `--source`. It
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

## Aider

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

## Amp

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

## Cline CLI

Cline CLI reads `~/.cline/data/sessions/sessions.db` and the referenced
`*.messages.json` artifacts. It extracts session timestamps/status, configured model,
visible user-turn boundaries, per-assistant token metrics, and tool names while
discarding prompts, responses, file contents, tool arguments/results, titles, Git
metadata, costs, credentials, and arbitrary metadata. Input totals include cache reads
and writes; normalized uncached input is the non-negative remainder. Model and event
timestamps come from each assistant artifact when present. The adapter was qualified
against Cline CLI's current SDK session schema in August 2026.

## Kimi Code CLI

Kimi Code CLI reads `~/.kimi/sessions/*/*/wire.jsonl`. It extracts turn boundaries,
step token usage, tool names, context-window samples, and completed compactions while
discarding user input, model output, reasoning, tool arguments/results, approvals,
notifications, subagent payloads, paths, and arbitrary events. The wire log does not
persist the selected model label, so calls use `unknown`; hashed work-directory keys
are not reversed. The adapter targets Wire v1 as qualified in August 2026.

## Amazon Q Developer CLI

Amazon Q Developer CLI reads persistent conversations from
`~/.local/share/amazon-q/data.sqlite3`. It extracts user-turn timestamps, model labels,
tool names, and request timing while discarding prompts, responses, transcripts,
environment context, tool inputs/results, paths except for explicit project matching,
request identifiers, credentials, auth state, and arbitrary metadata. Persistent
conversation state contains no token counters, so all normalized token values are zero.
Non-persistent sessions are unavailable. The adapter targets the current conversations
table and serialized state format as qualified in August 2026.

## Plandex

Plandex reads an offline copy of a self-hosted server's `PLANDEX_BASE_DIR`, specifically
`orgs/*/plans/*/conversation/*.json`. It extracts stable plan/message identifiers,
timestamps, roles, stop status, and provider-reported per-message token totals while
discarding messages, user IDs, subtasks, paths, code changes, flags, summaries,
PostgreSQL data, credentials, and Git history. Plandex does not split stored message
tokens by input/output or persist model/tool attribution in these files, so totals are
unattributed and models are `unknown`. Hosted Plandex accounts are not accessed.

Provider formats can change without notice. Unknown fields are ignored; malformed
provider records are counted and skipped. Compatibility fixes should add a fixture for
both the old and new format whenever possible.
