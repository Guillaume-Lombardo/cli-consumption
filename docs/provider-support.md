# Provider support

| Provider | Provider name | Aliases | Status | Default local source | Token semantics |
| --- | --- | --- | --- | --- | --- |
| Aider | `aider` | — | `supported` | `~/.aider/analytics.jsonl` | `additive` |
| Amazon Q Developer CLI | `amazon-q` | — | `supported` | `~/.local/share/amazon-q/data.sqlite3` | `unavailable` |
| Amp | `amp` | — | `supported` | `~/.local/share/amp/threads/` | `additive` |
| Claude Code | `claude` | `claude-code` | `supported` | `~/.claude/projects/` | `additive` |
| Cline CLI | `cline` | — | `supported` | `~/.cline/data/sessions/sessions.db` | `additive` |
| Codex | `codex` | — | `supported` | `~/.codex/sessions/` | `additive` |
| Continue CLI | `continue` | — | `supported` | `~/.continue/sessions/` | `additive` |
| Crush | `crush` | — | `supported` | `~/.local/share/crush/` | `context-snapshot` |
| Cursor CLI | `cursor` | — | `supported` | `~/.cursor/` | `unavailable` |
| Gemini CLI | `gemini` | — | `supported` | `~/.gemini/tmp/` | `additive` |
| GitHub Copilot CLI | `copilot` | — | `supported` | `~/.copilot/session-state/` | `conversation-aggregate` |
| Goose | `goose` | — | `supported` | `~/.local/share/goose/sessions/sessions.db` | `additive` |
| Grok Build | `grok` | — | `supported` | `~/.grok/sessions/` | `additive` |
| Kilo Code | `kilo` | — | `supported` | `~/.local/share/kilo/kilo.db` | `additive` |
| Kimi Code CLI | `kimi` | — | `supported` | `~/.kimi/sessions/` | `additive` |
| Mistral Vibe CLI | `mistral-vibe` | — | `supported` | `~/.vibe/logs/session/` | `conversation-aggregate` |
| OpenCode | `opencode` | — | `supported` | `~/.local/share/opencode/opencode.db` | `additive` |
| OpenHands CLI | `openhands` | — | `supported` | `~/.openhands/conversations/` | `additive` |
| Pi | `pi` | — | `supported` | `~/.pi/agent/sessions/` | `additive` |
| Plandex | `plandex` | — | `supported` | `/plandex-server` | `additive` |
| Qwen Code | `qwen` | — | `supported` | `~/.qwen/projects/` | `additive` |

“Supported” means the adapter has synthetic fixtures, extracts conversations, turns,
models, token usage, and tool names when available, and passes privacy regression tests.
It does not mean that token counters are equivalent to invoices.

`--provider all` detects every supported provider from its expected data directory and
ingests each metadata-only snapshot independently. With no explicit source it checks
the local default homes; repeated `--source` paths are filtered by detected format.

Provider metadata is maintained in one registry: canonical name, aliases, adapter,
default home, detection markers, support state, and token semantics. `providers --json`
uses that same registry to check default local stores and emits deterministic schema-v2
JSON. Only implemented, supported adapters appear in command output; planned adapters
do not. Token semantics are one of `additive`, `conversation-aggregate`,
`context-snapshot`, or `unavailable`; a missing counter is never presented as a measured
zero in provider capability output. Its
compatibility status is one of:

- `no-data`: no registered detection marker was found;
- `detected`: a store was recognized but contained no supported conversation;
- `compatible`: supported conversations parsed without malformed records;
- `degraded`: inspection failed safely or some records were malformed;
- `unsupported-schema`: the provider format was recognized but its schema is outside
  the adapter's supported range.

The diagnostic is deliberately coarse: it does not persist collected records or emit
local paths, provider identifiers, record counts, malformed values, or parser errors.

Collection failures expose only the canonical provider name and a fixed code:
`provider_limit_exceeded` for an input or snapshot safety bound,
`provider_format_incompatible` for a recognized but unsupported store schema, and
`invalid_snapshot` when normalized metadata violates the provider-neutral contract.
Unexpected adapter failures use `provider_collection_failed`. Exception messages,
paths, and record values are never included.

## Qualification ledger

The registry records the exact synthetic contract used to qualify every adapter. The
fixture links below contain generated test data rather than copied provider content;
the provenance link identifies the primary upstream project or documentation used to
verify the format. Qualification dates use UTC. A scheduled GitHub Actions check runs
weekly and fails once any entry is more than 90 days old, prompting maintainers to
recheck the upstream format and refresh its synthetic fixture before changing the
date. A passing age check does not prove that an undocumented local format has not
changed between releases.

| Provider name | Qualified version | Qualified on | Format | Synthetic fixture | Primary provenance | Qualification limits |
| --- | --- | --- | --- | --- | --- | --- |
| `aider` | analytics schema (unversioned) | `2026-08-30` | analytics JSONL | [fixture](../tests/test_aider_adapter.py) | [Aider](https://github.com/Aider-AI/aider/tree/5dc9490bb35f9729ef2c95d00a19ccd30c26339c) | Opt-in analytics; no projects, tools, cache, reasoning, or durations. |
| `amazon-q` | conversation state (unversioned) | `2026-08-30` | SQLite conversations and serialized state | [fixture](../tests/test_amazon_q_adapter.py) | [Amazon Q Developer CLI](https://github.com/aws/amazon-q-developer-cli/tree/15cc8f3cd18c4272925ce1c7053268eedff1ea0a) | Persistent conversations only; token counters unavailable. |
| `amp` | thread mirror (unversioned) | `2026-08-30` | thread JSON | [fixture](../tests/test_amp_adapter.py) | [Amp manual](https://web.archive.org/web/20260825165815id_/https://ampcode.com/manual) | No subthreads, compactions, reasoning split, or latency. |
| `codex` | rollout schema (unversioned) | `2026-08-30` | session rollout JSONL | [fixture](../tests/test_codex_adapter.py) | [Codex](https://github.com/openai/codex/tree/0a12b855a0b21068108a8a3b311d492712737e0f) | Local rollout metadata only; provider internals may evolve. |
| `copilot` | CLI 1.0.80 / event schema v1 | `2026-08-30` | session event JSONL | [fixture](../tests/test_copilot_adapter.py) | [GitHub Copilot CLI](https://github.com/github/copilot-cli/tree/v1.0.80) | Shutdown aggregates only; no per-turn token attribution. |
| `continue` | session schema (unversioned) | `2026-08-30` | session JSON | [fixture](../tests/test_continue_adapter.py) | [Continue](https://github.com/continuedev/continue/tree/5522c6f44ca0ac3528b37244818fbfa39b5af470) | No reliable message timing, context windows, compaction timing, or latency. |
| `crush` | CLI 0.91.2 | `2026-08-30` | project registry and additive SQLite migrations | [fixture](../tests/test_crush_adapter.py) | [Crush](https://github.com/charmbracelet/crush/tree/v0.91.2) | Latest context snapshot only; no additive per-call usage. |
| `cursor` | Composer 2 | `2026-08-30` | transcript JSONL and chat SQLite | [fixture](../tests/test_cursor_adapter.py) | [Cursor CLI](https://web.archive.org/web/20260815113223id_/https://cursor.com/docs/cli/overview) | No per-message timing or tokens; model attribution is incomplete. |
| `gemini` | session history (unversioned) | `2026-08-30` | active history JSON and JSONL | [fixture](../tests/test_gemini_adapter.py) | [Gemini CLI](https://github.com/google-gemini/gemini-cli/tree/0bd1d439751478771c45d3d0895a6a9760554bf4) | Nested agents excluded; hashed projects are not reversed. |
| `goose` | CLI 1.47.0 / schema v16 | `2026-08-30` | SQLite sessions and usage ledger | [fixture](../tests/test_goose_adapter.py) | [Goose](https://github.com/aaif-goose/goose/tree/v1.47.0) | Schema v16 only; no legacy JSONL, subagents, reasoning, or latency. |
| `grok` | session schema (unversioned) | `2026-08-30` | summary, updates, and events JSONL | [fixture](../tests/test_grok_adapter.py) | [Grok Build](https://github.com/xai-org/grok-build/tree/bc7f02eddd3d84085849dc19ed216f11c23b0571) | No costs, subagent relationships, rewinds, or manual compactions. |
| `claude` | transcript schema (unversioned) | `2026-08-30` | project session JSONL | [fixture](../tests/test_claude_adapter.py) | [Claude Code](https://github.com/anthropics/claude-code/tree/f1af9b1f4b1fd4c776135381606edada82ef638e) | Main sessions only; no subagents, context windows, effort, or latency. |
| `cline` | SDK session schema (unversioned) | `2026-08-30` | SQLite session index and message JSON | [fixture](../tests/test_cline_adapter.py) | [Cline](https://github.com/cline/cline/tree/48d63852745460ff0fa3dfcc0457bbe2493841de) | No costs or arbitrary task metadata; artifacts must remain present. |
| `kilo` | CLI 7.5.5 | `2026-08-30` | SQLite session, message, and part tables | [fixture](../tests/test_kilo_adapter.py) | [Kilo Code](https://github.com/Kilo-Org/kilocode/tree/v7.5.5) | CLI store only; no legacy IDE tasks, cloud sessions, or subagents. |
| `kimi` | Wire v1 | `2026-08-30` | wire event JSONL | [fixture](../tests/test_kimi_adapter.py) | [Kimi Code CLI](https://github.com/MoonshotAI/kimi-cli/tree/cbc15c076d17f70fec9f89c90c0502e68657f505) | Selected model unavailable; hashed work directories are not reversed. |
| `mistral-vibe` | CLI 2.24.5 | `2026-08-30` | session meta JSON and messages JSONL | [fixture](../tests/test_mistral_vibe_adapter.py) | [Mistral Vibe](https://github.com/mistralai/mistral-vibe/tree/v2.24.5) | Session aggregates only; no timing or historical model attribution. |
| `opencode` | CLI 1.18.23 / SQLite v2 | `2026-08-31` | SQLite session plus current message/part or projection records | [fixture](../tests/test_opencode_adapter.py) | [OpenCode](https://github.com/anomalyco/opencode/tree/v1.18.23) | No pre-v2 JSON, child sessions, context windows, or costs. |
| `openhands` | CLI 1.16.0 | `2026-08-30` | SDK base state and event JSON | [fixture](../tests/test_openhands_adapter.py) | [OpenHands](https://github.com/OpenHands/OpenHands/tree/v1.16.0) | Local SDK persistence only; no cloud conversations or delegates. |
| `pi` | session schema v3 | `2026-08-30` | branched session JSONL | [fixture](../tests/test_pi_adapter.py) | [Pi](https://github.com/earendil-works/pi/tree/853a80d26c90a14c1886f0ebb8ffaae133ca2185) | All branches counted; no branch graph, context windows, or durations. |
| `plandex` | conversation JSON (unversioned) | `2026-08-30` | self-hosted conversation JSON | [fixture](../tests/test_plandex_adapter.py) | [Plandex](https://github.com/plandex-ai/plandex/tree/e2d772072efadbe41d2946d97d79be55532dbab5) | Offline self-hosted copy only; models and tools unavailable. |
| `qwen` | CLI 0.22.2 | `2026-08-30` | active-branch chat JSONL | [fixture](../tests/test_qwen_adapter.py) | [Qwen Code](https://github.com/QwenLM/qwen-code/tree/v0.22.2) | Archived and sidechain sessions excluded; cache writes unavailable. |

Compatibility classification is provider-neutral and ordered: an explicit
`UnsupportedProviderFormat` is `unsupported-schema`; any other inspection failure or
any skipped malformed record is `degraded`; a clean snapshot with conversations is
`compatible`; and a recognized but empty store is `detected`. Diagnostic output never
includes the qualification provenance, fixture path, exception, or inspected values.

All adapters share the provider-input limits documented in the privacy boundary:
10,000 discovered candidates and 512 MiB of actual provider-file reads per collection;
64 MiB per monolithic JSON file; 256 MiB per JSONL file and 8 MiB per line; and, for
SQLite stores, 512 MiB cumulatively across databases plus active sidecars, 250,000
selected rows, 8 MiB per structured field, and 256 MiB across structured fields.
Exceeding a limit aborts collection with a generic code rather than silently producing
a partial snapshot.

## Mistral Vibe CLI

Mistral Vibe CLI reads top-level session directories under
`~/.vibe/logs/session/`, when interaction logging is enabled. The adapter was
qualified against Mistral Vibe 2.24.5 and its current `meta.json` plus
`messages.jsonl` format in August 2026. It extracts stable session and user-message
identifiers, session timestamps, the latest active model alias, cumulative token
usage, function names from assistant tool calls, and compaction markers. Working
directories are inspected only for explicit project mappings. Titles, prompts,
responses, reasoning, tool arguments/results, system instructions, usernames, Git
metadata, environment values other than the mapped working directory, prices,
arbitrary configuration, and raw messages are discarded.

Vibe persists prompt, cached-prompt, and completion counters only as cumulative
session statistics. The adapter therefore emits one unattributed aggregate model
call, subtracts cached prompt tokens from uncached input, and does not assign token
usage to individual turns. The persisted active model is the latest selection and is
not attributed historically to turns. Message records have no timestamps, so turns,
tools, and compactions do not expose event times or durations. Child-agent sessions,
model changes, cache writes, reasoning tokens, context-window samples, costs, tool
outcomes, and provider-reported latency are not collected. Vibe's internal session
format can change without notice, and local token events are not billing data.

## Codex

Codex additionally exposes provider-reported turn duration and TTFT, model context
window samples, bounded reasoning/collaboration/service-tier labels, timestamped
compactions, technical work-item categories and durations, and local thread-spawn
relationships. Work-item content and rate-limit payloads are deliberately excluded.
Provider-supplied nicknames are discarded, while subagent roles and statuses are
mapped to closed vocabularies and the source label is fixed to `local-jsonl`.
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
and reasoning counters. Cache semantics depend on the backend: Anthropic-compatible
usage reports uncached prompt tokens plus separate cache counters, while OpenAI-style
usage reports cache reads as a subset of prompt tokens. The adapter normalizes those
forms by provider, prefers per-response usage, and adds only the unattributed remainder
of the cumulative session snapshot, preventing double counting. Persisted
`conversationSummary` markers are reported as compactions without retaining summaries.
Session files do not persist reliable per-message timestamps, so model calls and turns
have no timestamps or durations; the file modification time is retained only as the
conversation's approximate end time. The adapter does not collect IDE-extension-only
history stores, context-window sizes, provider-reported status, latency, or cost.
Continue's internal session format can change without notice, and local token events
are not billing data.

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
`~/.local/share/opencode/`). For OpenCode 1.18.23, it reads assistant model references
and token usage from `message.data`, and tool names plus compaction markers from
`part.data`. When the current `message` and `part` tables are absent, it retains
compatibility with the earlier `session_message` projection format. If either current
table is present but incomplete, the adapter reports an unsupported schema instead of
falling back to a misleading zero-usage snapshot. Message text, reasoning, tool
inputs/results, shell commands/output, paths, titles, errors, costs, and arbitrary
metadata are discarded. Model labels combine OpenCode's provider and model identifiers.

OpenCode reports uncached input, cache reads, cache writes, visible output, and
reasoning separately. Normalized input and output totals include their respective
components. The adapter does not currently read pre-v2 JSON storage, child-session
relationships, context-window sizes, or provider-reported cost. The SQLite schema is
internal and may change without notice; local token events are not billing data.

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
