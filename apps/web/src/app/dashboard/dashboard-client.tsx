"use client";

import {
  createDashboardCalculations,
  type DashboardSlice,
} from "@cli-consumption/analytics";
import {
  type DashboardLayoutV1,
  type DashboardWidgetType,
  type DashboardWidgetV1,
  DEFAULT_DASHBOARD_LAYOUT_V1,
  dashboardLayoutComposition,
} from "@cli-consumption/contracts";
import { formatDuration, formatPercent } from "@cli-consumption/ui";
import {
  ActivityCatalog,
  Bars,
  DashboardLayoutGrid,
  Metric,
  Section,
} from "@cli-consumption/ui/react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useState,
} from "react";

import {
  type ConversationDetail,
  type ConversationPage,
  type ConversationSummary,
  type DashboardDatasetResponse,
  type DashboardQueryV1,
  fetchDashboardLayout,
  fetchOfflineExport,
  postReporting,
  queryForRange,
  type RangeChoice,
  saveDashboardLayout,
} from "../../lib/reporting";
import {
  addWidget,
  createLayoutHistory,
  layoutHistoryReducer,
  removeWidget,
  updateWidget,
} from "./layout-editor";
import {
  LayoutEditorToolbar,
  WidgetEditorControls,
  WidgetPalette,
} from "./layout-editor-view";

type FilterDimension = keyof DashboardQueryV1["filters"];
type CohortDimension =
  | "project"
  | "model"
  | "effort"
  | "mode"
  | "delegation"
  | "compaction";

const EMPTY_FILTERS: DashboardQueryV1["filters"] = {
  machines: [],
  models: [],
  projects: [],
  providers: [],
};

const ERROR_MESSAGES: Record<string, string> = {
  dashboard_response_limit_exceeded:
    "This selection is too large. Narrow the date range or filters.",
  pagination_expired: "The conversation page expired. Start again from the first page.",
  reporting_busy: "Reporting is busy. Try again in a moment.",
  export_unavailable: "Offline export is not configured for this dashboard.",
  reporting_limit_exceeded:
    "This selection exceeds the reporting limit. Narrow it and retry.",
  reporting_response_too_large:
    "The offline file is too large. Narrow the date range or filters and retry.",
  reporting_timeout: "Reporting timed out. Narrow the selection and retry.",
  reporting_unavailable: "Reporting is temporarily unavailable.",
};

function messageFor(error: unknown): string {
  const code = error instanceof Error ? error.message : "reporting_unavailable";
  return ERROR_MESSAGES[code] ?? ERROR_MESSAGES.reporting_unavailable;
}

function short(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function number(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Intl.NumberFormat("en").format(value);
}

function groupCount<T>(
  rows: readonly T[],
  label: (row: T) => string,
): Array<[string, number]> {
  const grouped = new Map<string, number>();
  for (const row of rows) {
    const key = label(row) || "unknown";
    grouped.set(key, (grouped.get(key) ?? 0) + 1);
  }
  return [...grouped].sort((left, right) => right[1] - left[1]);
}

function groupSum<T>(
  rows: readonly T[],
  label: (row: T) => string,
  value: (row: T) => number,
): Array<[string, number]> {
  const grouped = new Map<string, number>();
  for (const row of rows) {
    const key = label(row) || "unknown";
    grouped.set(key, (grouped.get(key) ?? 0) + value(row));
  }
  return [...grouped].sort((left, right) => right[1] - left[1]);
}

function Filters({
  custom,
  filters,
  options,
  range,
  setCustom,
  setFilter,
  setRange,
}: {
  custom: { from: string; to: string };
  filters: DashboardQueryV1["filters"];
  options: DashboardDatasetResponse["filters"];
  range: RangeChoice;
  setCustom: (value: { from: string; to: string }) => void;
  setFilter: (dimension: FilterDimension, value: string) => void;
  setRange: (value: RangeChoice) => void;
}) {
  return (
    <section className="filter-panel" aria-label="Dashboard filters">
      <label>
        Period
        <select
          value={range}
          onChange={(event) => setRange(event.target.value as RangeChoice)}
        >
          <option value="7">Latest 7 days</option>
          <option value="30">Latest 30 days</option>
          <option value="90">Latest 90 days</option>
          <option value="all">All history</option>
          <option value="custom">Custom range</option>
        </select>
      </label>
      {(["providers", "machines", "projects", "models"] as const).map((dimension) => (
        <label key={dimension}>
          {dimension.slice(0, 1).toUpperCase() + dimension.slice(1, -1)}
          <select
            value={filters[dimension][0] ?? ""}
            onChange={(event) => setFilter(dimension, event.target.value)}
          >
            <option value="">All</option>
            {options[dimension].map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      ))}
      {range === "custom" ? (
        <div className="custom-range">
          <label>
            From
            <input
              type="date"
              value={custom.from}
              onChange={(event) => setCustom({ ...custom, from: event.target.value })}
            />
          </label>
          <label>
            Until (exclusive)
            <input
              type="date"
              value={custom.to}
              onChange={(event) => setCustom({ ...custom, to: event.target.value })}
            />
          </label>
        </div>
      ) : null}
      <p className="filter-note">
        Operational labels stay out of the URL and are sent only in POST bodies.
      </p>
    </section>
  );
}

function MetricsView({
  data,
  editing,
  layout,
  onWidgetChange,
  onWidgetRemove,
  query,
  slice,
}: {
  data: DashboardDatasetResponse;
  editing: boolean;
  layout: DashboardLayoutV1;
  onWidgetChange: (
    id: string,
    delta: { height?: number; width?: number; x?: number; y?: number },
  ) => void;
  onWidgetRemove: (id: string) => void;
  query: DashboardQueryV1;
  slice: DashboardSlice;
}) {
  const calculations = useMemo(() => createDashboardCalculations(data), [data]);
  const semanticCalls = calculations.semanticTokenCalls(slice);
  const metrics = calculations.metrics({ ...slice, calls: semanticCalls });
  const closed = slice.turns.filter(
    (turn) => turn.status === "completed" || turn.status === "aborted",
  );
  const catalog = useMemo(
    () => calculations.chartCatalog(slice, calculations.rangeFor("all")),
    [calculations, slice],
  );
  const outcomes = groupCount(slice.turns, (turn) => turn.status);
  const workKinds = groupSum(
    slice.work,
    (item) => item.kind,
    (item) => item.durationMs ?? 0,
  );
  const contextPressures = slice.contexts
    .map((sample) => (100 * sample.inputTokens) / sample.contextWindowTokens)
    .filter(Number.isFinite);
  const compacted = new Set(slice.compactions.map((item) => item.conversationKey)).size;
  const delegated = new Set(slice.subagents.map((item) => item.conversationKey)).size;
  const durationCoverage = calculations.ratio(
    slice.turns.filter((turn) => turn.durationMs !== null).length,
    slice.turns.length,
  );
  const ttftCoverage = calculations.ratio(
    slice.turns.filter((turn) => turn.ttftMs !== null).length,
    slice.turns.length,
  );
  const unknownModels = calculations.ratio(
    semanticCalls.filter((call) => !call.model || call.model === "unknown").length,
    semanticCalls.length,
  );

  const widgets: Record<DashboardWidgetType, ReactNode> = {
    "headline-metrics": (
      <section className="metric-grid" aria-label="Headline metrics">
        <Metric
          label="Closed turns"
          value={number(metrics.completed + metrics.aborted)}
        />
        <Metric label="Active days" value={number(metrics.activeDays)} />
        <Metric label="Conversations" value={number(slice.conversations.length)} />
        <Metric
          label="Total tokens"
          value={short(metrics.tokens)}
          help="Provider counters; not billing data."
        />
        <Metric
          label="Median additive tokens / turn"
          value={short(metrics.tokensPerTurn)}
        />
        <Metric label="Median TTFT" value={formatDuration(metrics.ttftP50)} />
        <Metric label="Median duration" value={formatDuration(metrics.durationP50)} />
        <Metric label="Active time" value={formatDuration(metrics.activeMs)} />
        <Metric label="Daily token peak" value={short(catalog.dailyPeakTokens)} />
        <Metric label="Turn rate" value={`${metrics.throughput.toFixed(1)}/h`} />
        <Metric
          label="Context pressure p95"
          value={formatPercent(metrics.pressureP95)}
        />
      </section>
    ),
    activity: (
      <Section title="Activity" note="52 UTC weeks · missing days are not zero">
        <ActivityCatalog catalog={catalog} />
      </Section>
    ),
    tools: (
      <Section title="Tools" note="names only">
        <Bars rows={catalog.rankings.tools} />
      </Section>
    ),
    models: (
      <Section title="Models" note="provider-reported tokens">
        <Bars rows={catalog.rankings.models} value={short} />
      </Section>
    ),
    "turn-performance": (
      <Section title="Turn performance" note="p50 / p75 / p95">
        <div className="mini-grid">
          <Metric label="Duration p50" value={formatDuration(metrics.durationP50)} />
          <Metric label="Duration p95" value={formatDuration(metrics.durationP95)} />
          <Metric label="TTFT p50" value={formatDuration(metrics.ttftP50)} />
          <Metric label="TTFT p95" value={formatDuration(metrics.ttftP95)} />
        </div>
      </Section>
    ),
    "workflow-complexity": (
      <Section title="Workflow complexity" note="content-free metadata">
        <div className="mini-grid">
          <Metric
            label="Turns using tools"
            value={formatPercent(
              calculations.ratio(
                slice.turns.filter((turn) => turn.toolCalls > 0).length,
                slice.turns.length,
              ),
            )}
          />
          <Metric
            label="Compacted conversations"
            value={formatPercent(
              calculations.ratio(compacted, slice.conversations.length),
            )}
          />
          <Metric
            label="Delegating conversations"
            value={formatPercent(
              calculations.ratio(delegated, slice.conversations.length),
            )}
          />
          <Metric
            label="Peak concurrent turns"
            value={number(calculations.maxConcurrent(closed))}
          />
        </div>
      </Section>
    ),
    "turn-outcomes": (
      <Section title="Turn outcomes" note="technical status, not task quality">
        <Bars rows={outcomes} />
      </Section>
    ),
    "context-pressure": (
      <Section title="Context pressure" note="input / context window">
        <div className="mini-grid">
          <Metric
            label="Median pressure"
            value={formatPercent(calculations.percentile(contextPressures, 0.5))}
          />
          <Metric
            label="Pressure p95"
            value={formatPercent(calculations.percentile(contextPressures, 0.95))}
          />
          <Metric label="Context samples" value={number(slice.contexts.length)} />
          <Metric label="Turn configurations" value={number(slice.settings.length)} />
        </div>
      </Section>
    ),
    "technical-work-items": (
      <Section title="Technical work items" note="content-free intervals">
        <Bars rows={workKinds} value={formatDuration} />
      </Section>
    ),
    cohorts: <Cohorts calculations={calculations} slice={slice} />,
    "data-quality": (
      <Section title="Data quality" note="coverage and ingestion health">
        <div className="mini-grid">
          <Metric
            label="Turn duration coverage"
            value={formatPercent(durationCoverage)}
          />
          <Metric label="TTFT coverage" value={formatPercent(ttftCoverage)} />
          <Metric label="Unknown model events" value={formatPercent(unknownModels)} />
          <Metric
            label="Malformed records"
            value={number(
              slice.conversations.length
                ? data.ingestionRuns.reduce((sum, run) => sum + run.malformed, 0)
                : 0,
            )}
          />
        </div>
      </Section>
    ),
    "conversation-explorer": <ConversationExplorer query={query} />,
  };
  return (
    <DashboardLayoutGrid
      editing={editing}
      widgets={dashboardLayoutComposition(layout)}
      renderEditor={(widget) => (
        <WidgetEditorControls
          onChange={onWidgetChange}
          onRemove={onWidgetRemove}
          widget={widget as DashboardWidgetV1}
        />
      )}
      renderWidget={(type) => widgets[type as DashboardWidgetType]}
    />
  );
}

function Cohorts({
  calculations,
  slice,
}: {
  calculations: ReturnType<typeof createDashboardCalculations>;
  slice: DashboardSlice;
}) {
  const [dimension, setDimension] = useState<CohortDimension>("project");
  const rows = calculations.cohortComparison(slice, dimension);
  return (
    <Section title="Cohort comparison" note="correlation, not causality">
      <label className="inline-control">
        Break down by
        <select
          value={dimension}
          onChange={(event) => setDimension(event.target.value as CohortDimension)}
        >
          <option value="project">Project</option>
          <option value="model">Model</option>
          <option value="effort">Reasoning effort</option>
          <option value="mode">Collaboration mode</option>
          <option value="delegation">Delegation</option>
          <option value="compaction">Compaction</option>
        </select>
      </label>
      {rows.length ? (
        <section
          className="table-scroll"
          aria-label="Scrollable cohort comparison"
          // biome-ignore lint/a11y/noNoninteractiveTabindex: overflow regions need keyboard access in Safari.
          tabIndex={0}
        >
          <table>
            <caption className="sr-only">Cohort metric comparison</caption>
            <thead>
              <tr>
                <th>Cohort</th>
                <th>Closed turns</th>
                <th>Median duration</th>
                <th>Median tokens</th>
                <th>Abort rate</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label}>
                  <td>{row.label}</td>
                  <td>{number(row.turns)}</td>
                  <td>{formatDuration(row.durationP50)}</td>
                  <td>{short(row.tokensP50)}</td>
                  <td>{formatPercent(row.abortRate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : (
        <p className="empty">No cohort has enough data for this selection.</p>
      )}
    </Section>
  );
}

function ConversationExplorer({ query }: { query: DashboardQueryV1 }) {
  const [page, setPage] = useState<ConversationPage | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const loadPage = useCallback(
    async (nextCursor: string | null) => {
      setPending(true);
      setError("");
      setDetail(null);
      try {
        const result = await postReporting<ConversationPage>("conversations", {
          cursor: nextCursor,
          direction: "desc",
          pageSize: 25,
          query,
          sort: "startedAt",
        });
        setPage(result);
        setCursor(nextCursor);
      } catch (caught) {
        if (caught instanceof Error && caught.message === "session_expired") {
          window.location.assign("/login?reason=session");
          return;
        }
        setError(messageFor(caught));
        if (caught instanceof Error && caught.message === "pagination_expired")
          setCursor(null);
      } finally {
        setPending(false);
      }
    },
    [query],
  );

  useEffect(() => {
    void loadPage(null);
  }, [loadPage]);

  async function showDetail(conversation: ConversationSummary) {
    setPending(true);
    setError("");
    try {
      setDetail(
        await postReporting<ConversationDetail>("conversation", {
          conversationRef: conversation.conversationRef,
          query,
        }),
      );
    } catch (caught) {
      if (caught instanceof Error && caught.message === "session_expired") {
        window.location.assign("/login?reason=session");
        return;
      }
      setError(messageFor(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel explorer" aria-busy={pending}>
      <div className="panel-head">
        <h2>Conversation explorer</h2>
        <span>bounded pages · opaque references</span>
      </div>
      {error ? (
        <p className="callout error" role="alert">
          {error}
        </p>
      ) : null}
      {page?.items.length ? (
        <>
          <section
            className="table-scroll conversation-table"
            aria-label="Scrollable conversation page"
            // biome-ignore lint/a11y/noNoninteractiveTabindex: overflow regions need keyboard access in Safari.
            tabIndex={0}
          >
            <table>
              <caption className="sr-only">Conversation page</caption>
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Provider</th>
                  <th>Machine</th>
                  <th>Project</th>
                  <th>Models</th>
                  <th>Turns</th>
                  <th>Tokens</th>
                  <th>
                    <span className="sr-only">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((conversation) => (
                  <tr key={conversation.conversationRef}>
                    <td>
                      {conversation.startedAt
                        ? new Date(conversation.startedAt).toLocaleString()
                        : "Unknown"}
                    </td>
                    <td>{conversation.provider}</td>
                    <td>{conversation.machine}</td>
                    <td>{conversation.project}</td>
                    <td>{conversation.models.join(", ") || "Unknown"}</td>
                    <td>{number(conversation.turns)}</td>
                    <td>{short(conversation.total_tokens)}</td>
                    <td>
                      <button
                        className="secondary"
                        type="button"
                        onClick={() => void showDetail(conversation)}
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <div className="conversation-cards">
            {page.items.map((conversation) => (
              <article className="conversation-card" key={conversation.conversationRef}>
                <div>
                  <strong>{conversation.project}</strong>
                  <span>
                    {conversation.provider} · {conversation.machine}
                  </span>
                  <span>{conversation.models.join(", ") || "Unknown model"}</span>
                </div>
                <dl>
                  <div>
                    <dt>Turns</dt>
                    <dd>{number(conversation.turns)}</dd>
                  </div>
                  <div>
                    <dt>Tokens</dt>
                    <dd>{short(conversation.total_tokens)}</dd>
                  </div>
                </dl>
                <button
                  className="secondary"
                  type="button"
                  onClick={() => void showDetail(conversation)}
                >
                  Inspect
                </button>
              </article>
            ))}
          </div>
        </>
      ) : pending ? (
        <p className="empty">Loading conversations…</p>
      ) : (
        <p className="empty">No conversations match this selection.</p>
      )}
      <div className="pagination">
        {cursor ? (
          <button
            className="secondary"
            type="button"
            onClick={() => void loadPage(null)}
            disabled={pending}
          >
            First page
          </button>
        ) : null}
        {page?.nextCursor ? (
          <button
            type="button"
            onClick={() => void loadPage(page.nextCursor)}
            disabled={pending}
          >
            Next page
          </button>
        ) : null}
      </div>
      {detail ? <ConversationDetailView detail={detail} /> : null}
    </section>
  );
}

function ConversationDetailView({ detail }: { detail: ConversationDetail }) {
  const pressures = detail.contextSamples
    .map((sample) => (100 * sample.inputTokens) / sample.contextWindowTokens)
    .filter(Number.isFinite)
    .sort((left, right) => left - right);
  const p95 = pressures[Math.floor((pressures.length - 1) * 0.95)] ?? null;
  return (
    <section className="detail-card" aria-label="Conversation detail">
      <div className="panel-head">
        <h3>{detail.conversation.project}</h3>
        <span>{detail.conversation.provider}</span>
      </div>
      <div className="mini-grid">
        <Metric label="Turns" value={number(detail.turns.length)} />
        <Metric label="Model calls" value={number(detail.modelCalls.length)} />
        <Metric label="Tool calls" value={number(detail.toolCalls.length)} />
        <Metric label="Context p95" value={formatPercent(p95)} />
      </div>
      <h4>Tools in this conversation</h4>
      <Bars rows={groupCount(detail.toolCalls, (call) => call.tool ?? "unknown")} />
    </section>
  );
}

export function DashboardClient() {
  const [range, setRange] = useState<RangeChoice>("30");
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [data, setData] = useState<DashboardDatasetResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [exportProfile, setExportProfile] = useState<"detailed" | "share-safe">(
    "detailed",
  );
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [layoutError, setLayoutError] = useState("");
  const [layoutLoading, setLayoutLoading] = useState(true);
  const [layoutNotice, setLayoutNotice] = useState("");
  const [layoutRecoveryNeeded, setLayoutRecoveryNeeded] = useState(false);
  const [layoutEtag, setLayoutEtag] = useState<string | null>(null);
  const [layoutBaseline, setLayoutBaseline] = useState<DashboardLayoutV1>(
    DEFAULT_DASHBOARD_LAYOUT_V1,
  );
  const [layoutHistory, dispatchLayout] = useReducer(
    layoutHistoryReducer,
    DEFAULT_DASHBOARD_LAYOUT_V1,
    createLayoutHistory,
  );
  const [editingLayout, setEditingLayout] = useState(false);
  const [savingLayout, setSavingLayout] = useState(false);

  useEffect(() => {
    const parameters = new URLSearchParams(window.location.search);
    const requested = parameters.get("range");
    if (["7", "30", "90", "all", "custom"].includes(requested ?? "")) {
      setRange(requested as RangeChoice);
    }
    if (requested === "custom") {
      setCustom({ from: parameters.get("from") ?? "", to: parameters.get("to") ?? "" });
    }
    const storedTheme = window.localStorage.getItem("cli-consumption-theme");
    if (storedTheme === "light") setTheme("light");
  }, []);

  useEffect(() => {
    void fetchDashboardLayout()
      .then((saved) => {
        setLayoutBaseline(saved.layout);
        setLayoutEtag(saved.etag);
        dispatchLayout({ layout: saved.layout, type: "replace" });
        setLayoutError("");
        setLayoutRecoveryNeeded(false);
      })
      .catch((caught) => {
        if (caught instanceof Error && caught.message === "session_expired") {
          window.location.assign("/login?reason=session");
          return;
        }
        setLayoutError(
          "The saved layout could not be loaded. The default layout is displayed.",
        );
        setLayoutRecoveryNeeded(true);
      })
      .finally(() => setLayoutLoading(false));
  }, []);

  const layoutDirty = useMemo(
    () => JSON.stringify(layoutHistory.present) !== JSON.stringify(layoutBaseline),
    [layoutBaseline, layoutHistory.present],
  );

  function commitLayout(layout: DashboardLayoutV1 | null, message: string) {
    if (!layout) {
      setLayoutNotice("That change is blocked by the grid bounds or another widget.");
      return;
    }
    dispatchLayout({ layout, type: "commit" });
    setLayoutNotice(message);
  }

  function changeWidget(
    id: string,
    delta: { height?: number; width?: number; x?: number; y?: number },
  ) {
    const widget = layoutHistory.present.widgets.find((item) => item.id === id);
    if (!widget) return;
    commitLayout(
      updateWidget(layoutHistory.present, id, {
        position:
          delta.x || delta.y
            ? {
                x: widget.position.x + (delta.x ?? 0),
                y: widget.position.y + (delta.y ?? 0),
              }
            : widget.position,
        size:
          delta.width || delta.height
            ? {
                height: widget.size.height + (delta.height ?? 0),
                width: widget.size.width + (delta.width ?? 0),
              }
            : widget.size,
      }),
      "Draft updated.",
    );
  }

  async function persistLayout(etag = layoutEtag) {
    if (!etag) {
      setLayoutNotice(
        "Layout saving is unavailable. Reload the saved layout and retry.",
      );
      setLayoutRecoveryNeeded(true);
      return;
    }
    setSavingLayout(true);
    try {
      const saved = await saveDashboardLayout(layoutHistory.present, etag);
      setLayoutBaseline(saved.layout);
      setLayoutEtag(saved.etag);
      dispatchLayout({ layout: saved.layout, type: "replace" });
      setLayoutNotice("Layout saved.");
      setLayoutRecoveryNeeded(false);
      setEditingLayout(false);
    } catch (caught) {
      if (caught instanceof Error && caught.message === "session_expired") {
        window.location.assign("/login?reason=session");
        return;
      }
      const conflict = caught instanceof Error && caught.message === "layout_conflict";
      setLayoutRecoveryNeeded(true);
      setLayoutNotice(
        conflict
          ? "The saved layout changed elsewhere. Your draft is preserved."
          : "The layout could not be saved. Your draft is preserved.",
      );
    } finally {
      setSavingLayout(false);
    }
  }

  async function reloadSavedLayout(preserveDraft: boolean) {
    try {
      const saved = await fetchDashboardLayout();
      setLayoutBaseline(saved.layout);
      setLayoutEtag(saved.etag);
      if (!preserveDraft) dispatchLayout({ layout: saved.layout, type: "replace" });
      setLayoutError("");
      setLayoutRecoveryNeeded(false);
      setLayoutNotice(
        preserveDraft
          ? "Latest revision loaded. Your draft is ready to retry."
          : "Saved layout reloaded.",
      );
      return saved.etag;
    } catch (caught) {
      if (caught instanceof Error && caught.message === "session_expired") {
        window.location.assign("/login?reason=session");
        return null;
      }
      setLayoutNotice(
        "The saved layout could not be reloaded. Your draft is preserved.",
      );
      setLayoutRecoveryNeeded(true);
      return null;
    }
  }

  async function retryLayoutSave() {
    const latest = await reloadSavedLayout(true);
    if (latest) await persistLayout(latest);
  }

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("cli-consumption-theme", theme);
  }, [theme]);

  useEffect(() => {
    const parameters = new URLSearchParams();
    parameters.set("range", range);
    if (range === "custom") {
      if (custom.from) parameters.set("from", custom.from);
      if (custom.to) parameters.set("to", custom.to);
    }
    window.history.replaceState(null, "", `/dashboard?${parameters.toString()}`);
  }, [custom, range]);

  const query = useMemo(
    () => queryForRange(range, filters, custom),
    [custom, filters, range],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void postReporting<DashboardDatasetResponse>("dashboard", query, controller.signal)
      .then(setData)
      .catch((caught) => {
        if (controller.signal.aborted) return;
        if (caught instanceof Error && caught.message === "session_expired") {
          window.location.assign("/login?reason=session");
          return;
        }
        setError(messageFor(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [query]);

  const slice = useMemo(() => {
    if (!data) return null;
    const calculations = createDashboardCalculations(data);
    return calculations.selectSlice({
      machine: "",
      model: "",
      project: "",
      provider: "",
      range: calculations.rangeFor("all"),
    });
  }, [data]);

  function setFilter(dimension: FilterDimension, value: string) {
    setFilters((current) => ({ ...current, [dimension]: value ? [value] : [] }));
  }

  async function signOut() {
    try {
      const response = await fetch("/api/session", { method: "DELETE" });
      if (response.ok || response.status === 401) {
        window.location.assign("/login");
        return;
      }
    } catch {
      // The fixed message below intentionally omits transport details.
    }
    setError("Sign-out failed. Check the connection and try again.");
  }

  async function exportOffline() {
    setExporting(true);
    setExportError("");
    try {
      const blob = await fetchOfflineExport({ ...query, profile: exportProfile });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "cli-consumption-dashboard.html";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      if (caught instanceof Error && caught.message === "session_expired") {
        window.location.assign("/login?reason=session");
        return;
      }
      setExportError(messageFor(caught));
    } finally {
      setExporting(false);
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="hero">
        <div className="hero-intro">
          <h1>CLI Consumption</h1>
          <div className="hero-copy">
            <p className="eyebrow">Local-first AI CLI observability</p>
            <p>
              Activity, responsiveness, token composition, and workflows — never
              prompts, responses, or tool arguments.
            </p>
          </div>
        </div>
        <div className="hero-actions">
          {!editingLayout ? (
            <button
              aria-busy={layoutLoading}
              className="secondary"
              disabled={layoutLoading}
              type="button"
              onClick={() => {
                dispatchLayout({ layout: layoutBaseline, type: "replace" });
                setEditingLayout(true);
                setLayoutNotice("Edit mode enabled.");
              }}
            >
              Edit dashboard
            </button>
          ) : null}
          <label className="inline-control">
            <span>Offline profile</span>
            <select
              aria-label="Offline export profile"
              value={exportProfile}
              onChange={(event) =>
                setExportProfile(event.target.value as "detailed" | "share-safe")
              }
            >
              <option value="detailed">Detailed</option>
              <option value="share-safe">Share-safe</option>
            </select>
          </label>
          <button
            className="secondary"
            disabled={exporting}
            type="button"
            onClick={() => void exportOffline()}
          >
            {exporting ? "Exporting…" : "Export offline"}
          </button>
          <button
            className="secondary"
            type="button"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </button>
          <button className="secondary" type="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>
      {exportError ? (
        <section className="callout error" role="alert">
          <strong>Offline export failed.</strong>
          <span>{exportError}</span>
        </section>
      ) : null}
      {layoutError ? (
        <section className="callout" role="status" aria-live="polite">
          <strong>Saved layout unavailable.</strong>
          <span>{layoutError}</span>
          {layoutRecoveryNeeded && !layoutNotice ? (
            <span className="layout-conflict-actions">
              <button
                className="secondary"
                type="button"
                onClick={() => void reloadSavedLayout(false)}
              >
                Reload saved layout
              </button>
              <button type="button" onClick={() => void retryLayoutSave()}>
                Retry with latest revision
              </button>
            </span>
          ) : null}
        </section>
      ) : null}
      {editingLayout ? (
        <LayoutEditorToolbar
          canRedo={layoutHistory.future.length > 0}
          canUndo={layoutHistory.past.length > 0}
          dirty={layoutDirty}
          onCancel={() => {
            dispatchLayout({ layout: layoutBaseline, type: "replace" });
            setEditingLayout(false);
            setLayoutNotice("Layout changes discarded.");
          }}
          onRedo={() => dispatchLayout({ type: "redo" })}
          onReset={() => {
            dispatchLayout({ type: "reset" });
            setLayoutNotice("Default layout applied to the draft. You can undo it.");
          }}
          onSave={() => void persistLayout()}
          onUndo={() => dispatchLayout({ type: "undo" })}
          saving={savingLayout}
        />
      ) : null}
      {layoutNotice ? (
        <section className="callout" role="status" aria-live="polite">
          <span>{layoutNotice}</span>
          {layoutRecoveryNeeded ? (
            <span className="layout-conflict-actions">
              <button
                className="secondary"
                type="button"
                onClick={() => void reloadSavedLayout(false)}
              >
                Reload saved layout
              </button>
              <button type="button" onClick={() => void retryLayoutSave()}>
                Retry with latest revision
              </button>
            </span>
          ) : null}
        </section>
      ) : null}
      {editingLayout ? (
        <WidgetPalette
          layout={layoutHistory.present}
          onAdd={(type) =>
            commitLayout(
              addWidget(layoutHistory.present, type),
              "Widget added to the draft.",
            )
          }
        />
      ) : null}
      <Filters
        custom={custom}
        filters={filters}
        options={data?.filters ?? EMPTY_FILTERS}
        range={range}
        setCustom={setCustom}
        setFilter={setFilter}
        setRange={setRange}
      />
      {error ? (
        <section className="callout error" role="alert">
          <strong>Reporting could not load.</strong>
          <span>{error}</span>
        </section>
      ) : null}
      {loading ? (
        <p className="callout" role="status">
          Loading the bounded selection…
        </p>
      ) : null}
      {!loading && data && slice ? (
        slice.conversations.length ? (
          <MetricsView
            data={data}
            editing={editingLayout}
            layout={editingLayout ? layoutHistory.present : layoutBaseline}
            onWidgetChange={changeWidget}
            onWidgetRemove={(id) =>
              commitLayout(
                removeWidget(layoutHistory.present, id),
                "Widget removed from the draft.",
              )
            }
            query={query}
            slice={slice}
          />
        ) : (
          <section className="empty-state">
            <h2>No activity in this selection</h2>
            <p>Choose a wider period or clear one of the operational filters.</p>
          </section>
        )
      ) : null}
    </main>
  );
}
