import {
  assertDashboardLayoutV1,
  DASHBOARD_GRID_COLUMNS,
  DASHBOARD_GRID_ROWS,
  DASHBOARD_WIDGET_REGISTRY,
  DEFAULT_DASHBOARD_LAYOUT_V1,
  MAX_DASHBOARD_WIDGETS,
  type DashboardLayoutV1,
  type DashboardWidgetType,
  type DashboardWidgetV1,
} from "@cli-consumption/contracts";

export const LAYOUT_HISTORY_LIMIT = 20;

export interface LayoutHistory {
  future: DashboardLayoutV1[];
  past: DashboardLayoutV1[];
  present: DashboardLayoutV1;
}

export type LayoutHistoryAction =
  | { type: "replace"; layout: DashboardLayoutV1 }
  | { type: "commit"; layout: DashboardLayoutV1 }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "reset" };

export const WIDGET_CATALOG: Record<
  DashboardWidgetType,
  { description: string; metrics: string; title: string }
> = {
  "headline-metrics": {
    description: "Compact overview of the selected reporting period.",
    metrics: "turns, conversations, tokens, duration, coverage",
    title: "Headline metrics",
  },
  activity: {
    description: "UTC activity calendar, streaks, and bounded time series.",
    metrics: "daily activity, token composition, provider/model series",
    title: "Activity",
  },
  tools: {
    description: "Exact ranking of the tool names present in the dataset.",
    metrics: "tool call count",
    title: "Tools",
  },
  models: {
    description: "Sober ranking of provider-reported model usage.",
    metrics: "attributable tokens by model",
    title: "Models",
  },
  "turn-performance": {
    description: "Latency and duration percentiles for completed work.",
    metrics: "duration and time-to-first-token percentiles",
    title: "Turn performance",
  },
  "workflow-complexity": {
    description: "Content-free workflow structure and concurrency.",
    metrics: "tool use, compaction, delegation, concurrency",
    title: "Workflow complexity",
  },
  "turn-outcomes": {
    description: "Technical completion states, never task quality.",
    metrics: "turn status counts",
    title: "Turn outcomes",
  },
  "context-pressure": {
    description: "Input-token pressure against known context windows.",
    metrics: "context pressure and sample coverage",
    title: "Context pressure",
  },
  "technical-work-items": {
    description: "Content-free technical work intervals by category.",
    metrics: "work-item duration",
    title: "Technical work items",
  },
  cohorts: {
    description: "Bounded comparisons across available operational cohorts.",
    metrics: "duration, tokens, tools, pressure, status",
    title: "Cohort comparison",
  },
  "data-quality": {
    description: "Coverage and malformed-record indicators.",
    metrics: "duration, TTFT, model, ingestion coverage",
    title: "Data quality",
  },
  "conversation-explorer": {
    description: "Accessible exact table for the current selection.",
    metrics: "conversation metadata and detail",
    title: "Conversation explorer",
  },
};

function clone(layout: DashboardLayoutV1): DashboardLayoutV1 {
  return structuredClone(layout);
}

/** Create a bounded in-memory history without browser persistence. */
export function createLayoutHistory(layout: DashboardLayoutV1): LayoutHistory {
  assertDashboardLayoutV1(layout);
  return { future: [], past: [], present: clone(layout) };
}

/** Apply undoable layout edits while retaining at most twenty prior drafts. */
export function layoutHistoryReducer(
  state: LayoutHistory,
  action: LayoutHistoryAction,
): LayoutHistory {
  if (action.type === "replace") return createLayoutHistory(action.layout);
  if (action.type === "reset") {
    return layoutHistoryReducer(state, {
      layout: clone(DEFAULT_DASHBOARD_LAYOUT_V1),
      type: "commit",
    });
  }
  if (action.type === "undo") {
    const previous = state.past.at(-1);
    if (!previous) return state;
    return {
      future: [clone(state.present), ...state.future].slice(0, LAYOUT_HISTORY_LIMIT),
      past: state.past.slice(0, -1),
      present: clone(previous),
    };
  }
  if (action.type === "redo") {
    const next = state.future[0];
    if (!next) return state;
    return {
      future: state.future.slice(1),
      past: [...state.past, clone(state.present)].slice(-LAYOUT_HISTORY_LIMIT),
      present: clone(next),
    };
  }
  assertDashboardLayoutV1(action.layout);
  if (JSON.stringify(action.layout) === JSON.stringify(state.present)) return state;
  return {
    future: [],
    past: [...state.past, clone(state.present)].slice(-LAYOUT_HISTORY_LIMIT),
    present: clone(action.layout),
  };
}

function overlaps(
  candidate: Pick<DashboardWidgetV1, "position" | "size">,
  widget: DashboardWidgetV1,
): boolean {
  return (
    candidate.position.x < widget.position.x + widget.size.width &&
    candidate.position.x + candidate.size.width > widget.position.x &&
    candidate.position.y < widget.position.y + widget.size.height &&
    candidate.position.y + candidate.size.height > widget.position.y
  );
}

/** Return the first row-major free position for the registry minimum size. */
export function firstFitWidget(
  layout: DashboardLayoutV1,
  type: DashboardWidgetType,
): DashboardWidgetV1 | null {
  if (layout.widgets.length >= MAX_DASHBOARD_WIDGETS) return null;
  const limits = DASHBOARD_WIDGET_REGISTRY[type];
  const occupied = new Set(layout.widgets.map((widget) => widget.id));
  const id = !occupied.has(type)
    ? type
    : Array.from(
        { length: MAX_DASHBOARD_WIDGETS },
        (_, index) => `${type}-${index + 1}`,
      ).find((candidate) => !occupied.has(candidate));
  if (!id) return null;
  const size = { height: limits.minHeight, width: limits.minWidth };
  for (let y = 0; y + size.height <= DASHBOARD_GRID_ROWS; y += 1) {
    for (let x = 0; x + size.width <= DASHBOARD_GRID_COLUMNS; x += 1) {
      const candidate: DashboardWidgetV1 = {
        config: {},
        id,
        position: { x, y },
        size,
        type,
      };
      if (!layout.widgets.some((widget) => overlaps(candidate, widget)))
        return candidate;
    }
  }
  return null;
}

/** Return a strictly valid draft update, or null for collisions and grid violations. */
export function updateWidget(
  layout: DashboardLayoutV1,
  id: string,
  update: Partial<Pick<DashboardWidgetV1, "position" | "size">>,
): DashboardLayoutV1 | null {
  const next = clone(layout);
  const widget = next.widgets.find((candidate) => candidate.id === id);
  if (!widget) return null;
  if (update.position) widget.position = { ...update.position };
  if (update.size) widget.size = { ...update.size };
  try {
    assertDashboardLayoutV1(next);
    return next;
  } catch {
    return null;
  }
}

/** Remove a widget while preserving the v1 non-empty layout invariant. */
export function removeWidget(
  layout: DashboardLayoutV1,
  id: string,
): DashboardLayoutV1 | null {
  if (layout.widgets.length <= 1) return null;
  const next = {
    ...clone(layout),
    widgets: layout.widgets.filter((item) => item.id !== id),
  };
  if (next.widgets.length === layout.widgets.length) return null;
  assertDashboardLayoutV1(next);
  return next;
}

/** Add one registered widget at the deterministic first free grid position. */
export function addWidget(
  layout: DashboardLayoutV1,
  type: DashboardWidgetType,
): DashboardLayoutV1 | null {
  const widget = firstFitWidget(layout, type);
  if (!widget) return null;
  const next = { ...clone(layout), widgets: [...layout.widgets, widget] };
  assertDashboardLayoutV1(next);
  return next;
}
