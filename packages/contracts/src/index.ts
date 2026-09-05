export const DASHBOARD_DATASET_VERSION = 1 as const;
export const DASHBOARD_LAYOUT_VERSION = 1 as const;
export const DASHBOARD_GRID_COLUMNS = 12 as const;
export const DASHBOARD_GRID_ROWS = 64 as const;
export const MAX_DASHBOARD_WIDGETS = 32 as const;
export const MAX_DASHBOARD_LAYOUT_BYTES = 64 * 1024;

export const DASHBOARD_WIDGET_REGISTRY = {
  "headline-metrics": { minWidth: 12, maxWidth: 12, minHeight: 1, maxHeight: 2 },
  activity: { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  tools: { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  models: { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  "turn-performance": { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  "workflow-complexity": { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  "turn-outcomes": { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  "context-pressure": { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  "technical-work-items": { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  cohorts: { minWidth: 6, maxWidth: 12, minHeight: 1, maxHeight: 6 },
  "data-quality": { minWidth: 3, maxWidth: 12, minHeight: 1, maxHeight: 4 },
  "conversation-explorer": {
    minWidth: 12,
    maxWidth: 12,
    minHeight: 2,
    maxHeight: 8,
  },
} as const;

export type DashboardWidgetType = keyof typeof DASHBOARD_WIDGET_REGISTRY;

export interface DashboardWidgetV1 {
  id: string;
  type: DashboardWidgetType;
  position: { x: number; y: number };
  size: { width: number; height: number };
  config: Record<string, never>;
}

export interface DashboardLayoutV1 {
  version: typeof DASHBOARD_LAYOUT_VERSION;
  columns: typeof DASHBOARD_GRID_COLUMNS;
  widgets: DashboardWidgetV1[];
}

const DEFAULT_WIDGET_TYPES: readonly DashboardWidgetType[] = [
  "headline-metrics",
  "activity",
  "tools",
  "models",
  "turn-performance",
  "workflow-complexity",
  "turn-outcomes",
  "context-pressure",
  "technical-work-items",
  "cohorts",
  "data-quality",
  "conversation-explorer",
];

/** Canonical replacement for the pre-layout dashboard composition. */
export const DEFAULT_DASHBOARD_LAYOUT_V1: DashboardLayoutV1 = {
  version: DASHBOARD_LAYOUT_VERSION,
  columns: DASHBOARD_GRID_COLUMNS,
  widgets: DEFAULT_WIDGET_TYPES.map((type, index) => {
    const full = type === "headline-metrics" || type === "conversation-explorer";
    return {
      id: type,
      type,
      position: {
        x: full || index % 2 === 1 ? 0 : 6,
        y: index === 0 ? 0 : Math.ceil(index / 2),
      },
      size: { width: full ? 12 : 6, height: type === "conversation-explorer" ? 2 : 1 },
      config: {},
    };
  }),
};

const INSTANCE_SUFFIXES = new Set(
  Array.from({ length: MAX_DASHBOARD_WIDGETS }, (_, index) => String(index + 1)),
);
const LAYOUT_KEYS = ["version", "columns", "widgets"];
const WIDGET_KEYS = ["id", "type", "position", "size", "config"];

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  return (
    actual.length === keys.length &&
    [...keys].sort().every((key, i) => key === actual[i])
  );
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Strictly validate layouts accepted for persistence. */
export function assertDashboardLayoutV1(
  value: unknown,
): asserts value is DashboardLayoutV1 {
  if (!object(value) || !exactKeys(value, LAYOUT_KEYS))
    throw new TypeError("invalid_dashboard_layout");
  if (
    value.version !== DASHBOARD_LAYOUT_VERSION ||
    value.columns !== DASHBOARD_GRID_COLUMNS ||
    !Array.isArray(value.widgets) ||
    value.widgets.length < 1 ||
    value.widgets.length > MAX_DASHBOARD_WIDGETS
  )
    throw new TypeError("invalid_dashboard_layout");
  const ids = new Set<string>();
  for (const candidate of value.widgets) {
    if (!object(candidate) || !exactKeys(candidate, WIDGET_KEYS))
      throw new TypeError("invalid_dashboard_layout");
    const type = candidate.type;
    const limits =
      typeof type === "string" && Object.hasOwn(DASHBOARD_WIDGET_REGISTRY, type)
        ? DASHBOARD_WIDGET_REGISTRY[type as DashboardWidgetType]
        : undefined;
    if (typeof candidate.id !== "string" || ids.has(candidate.id) || !limits)
      throw new TypeError("invalid_dashboard_layout");
    const prefix = `${type}-`;
    if (
      candidate.id !== type &&
      (!candidate.id.startsWith(prefix) ||
        !INSTANCE_SUFFIXES.has(candidate.id.slice(prefix.length)))
    )
      throw new TypeError("invalid_dashboard_layout");
    ids.add(candidate.id);
    if (
      !object(candidate.position) ||
      !exactKeys(candidate.position, ["x", "y"]) ||
      !object(candidate.size) ||
      !exactKeys(candidate.size, ["width", "height"]) ||
      !object(candidate.config) ||
      Object.keys(candidate.config).length !== 0
    )
      throw new TypeError("invalid_dashboard_layout");
    const { x, y } = candidate.position;
    const { width, height } = candidate.size;
    if (
      typeof x !== "number" ||
      typeof y !== "number" ||
      typeof width !== "number" ||
      typeof height !== "number" ||
      ![x, y, width, height].every(Number.isSafeInteger) ||
      x < 0 ||
      y < 0 ||
      width < limits.minWidth ||
      width > limits.maxWidth ||
      height < limits.minHeight ||
      height > limits.maxHeight ||
      x + width > DASHBOARD_GRID_COLUMNS ||
      y + height > DASHBOARD_GRID_ROWS
    )
      throw new TypeError("invalid_dashboard_layout");
  }
  for (let left = 0; left < value.widgets.length; left += 1) {
    for (let right = left + 1; right < value.widgets.length; right += 1) {
      const a = value.widgets[left];
      const b = value.widgets[right];
      if (
        a.position.x < b.position.x + b.size.width &&
        a.position.x + a.size.width > b.position.x &&
        a.position.y < b.position.y + b.size.height &&
        a.position.y + a.size.height > b.position.y
      )
        throw new TypeError("invalid_dashboard_layout");
    }
  }
}

/** Resolve stored layouts after a widget retirement, preserving deterministic order. */
export function resolveDashboardLayoutV1(value: unknown): DashboardLayoutV1 {
  if (
    !object(value) ||
    value.version !== 1 ||
    value.columns !== 12 ||
    !Array.isArray(value.widgets)
  )
    return structuredClone(DEFAULT_DASHBOARD_LAYOUT_V1);
  const known = value.widgets.filter(
    (widget) =>
      object(widget) &&
      typeof widget.type === "string" &&
      Object.hasOwn(DASHBOARD_WIDGET_REGISTRY, widget.type),
  );
  try {
    const candidate = { ...value, widgets: known };
    assertDashboardLayoutV1(candidate);
    return structuredClone(candidate);
  } catch {
    return structuredClone(DEFAULT_DASHBOARD_LAYOUT_V1);
  }
}

/** Return the canonical logical/rendering order implied by grid coordinates. */
export function dashboardLayoutComposition(value: unknown): DashboardWidgetV1[] {
  return [...resolveDashboardLayoutV1(value).widgets].sort(
    (left, right) =>
      left.position.y - right.position.y ||
      left.position.x - right.position.x ||
      left.id.localeCompare(right.id),
  );
}

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

export const MAX_TOKEN_SERIES_LABEL_BUCKETS = 5 as const;

export type ActivityMetric = "tokens" | "turns" | "conversations" | "duration";
export type TokenBreakdownDimension = "provider" | "model";
export type TokenSeriesBucketId =
  | `${TokenBreakdownDimension}:label:${number}`
  | `${TokenBreakdownDimension}:remainder`;

/** One calendar day; unobserved cells distinguish missing coverage from a zero. */
export interface ActivityDay {
  date: string;
  values: Partial<Record<ActivityMetric, number>>;
  observed: boolean;
}

/** A bounded category in one token-series day. IDs never reuse display labels. */
export interface TokenSeriesBucket {
  id: TokenSeriesBucketId;
  kind: "label" | "remainder";
  label: string;
  value: number;
}

/** Daily token total plus bounded provider and model category projections. */
export interface TokenSeriesPoint {
  date: string;
  total: number;
  providers: TokenSeriesBucket[];
  models: TokenSeriesBucket[];
}

/** Provider-neutral chart output consumed unchanged by online and offline renderers. */
export interface DashboardChartCatalog {
  days: ActivityDay[];
  availableMetrics: ActivityMetric[];
  currentStreak: number;
  longestStreak: number;
  dailyPeakTokens: number | null;
  tokenComposition: Array<[string, number]>;
  tokenSeries: TokenSeriesPoint[];
  availableBreakdowns: TokenBreakdownDimension[];
  rankings: {
    models: Array<[string, number]>;
    providers: Array<[string, number]>;
    projects: Array<[string, number]>;
    tools: Array<[string, number]>;
  };
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
