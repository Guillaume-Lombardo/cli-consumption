export const DASHBOARD_DATASET_VERSION = 1 as const;

export type NullableTimestamp = string | null;
export type LocalKey = number;
export type TokenSemantics =
  | "additive"
  | "conversation-aggregate"
  | "context-snapshot"
  | "unavailable";

export interface TokenComposition {
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  uncached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  visible_output_tokens: number;
  unattributed_tokens: number;
  total_tokens: number;
}

export interface DashboardConversation extends TokenComposition {
  key: LocalKey;
  provider: string;
  tokenSemantics: TokenSemantics;
  machine: string;
  project: string;
  startedAt: NullableTimestamp;
  endedAt: NullableTimestamp;
  durationSeconds: number | null;
  models: string[];
  turns: number;
  modelCalls: number;
  toolCalls: number;
  compactions: number;
}

export interface DashboardTurn extends TokenComposition {
  key: LocalKey;
  conversationKey: LocalKey;
  startedAt: NullableTimestamp;
  endedAt: NullableTimestamp;
  status: string;
  durationMs: number | null;
  ttftMs: number | null;
  modelCalls: number;
  toolCalls: number;
}

export interface DashboardModelCall extends TokenComposition {
  conversationKey: LocalKey;
  turnKey: LocalKey | null;
  timestamp: NullableTimestamp;
  model: string;
}

export interface DashboardToolCall {
  conversationKey: LocalKey;
  turnKey: LocalKey | null;
  sequence: number;
  timestamp: NullableTimestamp;
  tool: string | null;
}

export interface DashboardWorkItem {
  conversationKey: LocalKey;
  turnKey: LocalKey | null;
  kind: string;
  tool: string | null;
  startedAtMs: number | null;
  durationMs: number | null;
  status: string;
}

export interface DashboardContextSample {
  conversationKey: LocalKey;
  turnKey: LocalKey | null;
  timestamp: NullableTimestamp;
  inputTokens: number;
  contextWindowTokens: number;
}

export interface DashboardTurnSetting {
  conversationKey: LocalKey;
  turnKey: LocalKey | null;
  model: string | null;
  effort: string | null;
  mode: string | null;
  tier: string | null;
  contextWindowTokens: number | null;
}

export interface DashboardCompaction {
  conversationKey: LocalKey;
  turnKey: LocalKey | null;
  timestamp: NullableTimestamp;
}

export interface DashboardSubagent {
  conversationKey: LocalKey | null;
  childConversationKey: LocalKey | null;
  provider: string;
  machine: string;
  status: string;
  createdAtMs: number | null;
  updatedAtMs: number | null;
  role: string;
  tokens: number | null;
}

export interface DashboardIngestionRun {
  provider: string;
  ingestedAt: NullableTimestamp;
  received: number;
  written: number;
  skipped: number;
  malformed: number;
  duplicates: number;
}

export interface DashboardDatasetV1 {
  contractVersion: typeof DASHBOARD_DATASET_VERSION;
  meta: {
    shareSafe: boolean;
    exportWindow?: { since: NullableTimestamp; until: NullableTimestamp };
  };
  conversations: DashboardConversation[];
  turns: DashboardTurn[];
  modelCalls: DashboardModelCall[];
  toolCalls: DashboardToolCall[];
  workItems: DashboardWorkItem[];
  contextSamples: DashboardContextSample[];
  turnSettings: DashboardTurnSetting[];
  compactions: DashboardCompaction[];
  subagents: DashboardSubagent[];
  ingestionRuns: DashboardIngestionRun[];
  window?: { since: NullableTimestamp; until: NullableTimestamp };
  profile?: "detailed" | "share-safe";
  filters?: {
    providers: string[];
    machines: string[];
    projects: string[];
    models: string[];
  };
}

const DATASET_SECTIONS = [
  "conversations",
  "turns",
  "modelCalls",
  "toolCalls",
  "workItems",
  "contextSamples",
  "turnSettings",
  "compactions",
  "subagents",
  "ingestionRuns",
] as const;

/** Fail closed before analytics consume an unknown or incomplete contract. */
export function assertDashboardDatasetV1(
  value: unknown,
): asserts value is DashboardDatasetV1 {
  if (typeof value !== "object" || value === null) {
    throw new TypeError("invalid_dashboard_dataset");
  }
  const candidate = value as Record<string, unknown>;
  const meta = candidate.meta;
  if (
    candidate.contractVersion !== DASHBOARD_DATASET_VERSION ||
    typeof meta !== "object" ||
    meta === null ||
    typeof (meta as Record<string, unknown>).shareSafe !== "boolean" ||
    DATASET_SECTIONS.some((section) => !Array.isArray(candidate[section]))
  ) {
    throw new TypeError("invalid_dashboard_dataset");
  }
}
