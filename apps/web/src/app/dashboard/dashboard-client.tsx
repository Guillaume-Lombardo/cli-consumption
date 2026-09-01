"use client";

import {
  createDashboardCalculations,
  type DashboardSlice,
} from "@cli-consumption/analytics";
import { formatDuration, formatPercent } from "@cli-consumption/ui";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  type ConversationDetail,
  type ConversationPage,
  type ConversationSummary,
  type DashboardDatasetResponse,
  type DashboardQueryV1,
  type RangeChoice,
  postReporting,
  queryForRange,
} from "../../lib/reporting";

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
  reporting_limit_exceeded:
    "This selection exceeds the reporting limit. Narrow it and retry.",
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

function Metric({
  help,
  label,
  value,
}: {
  help?: string;
  label: string;
  value: string;
}) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {help ? <p>{help}</p> : null}
    </article>
  );
}

function Bars({
  rows,
  value = number,
}: {
  rows: Array<[string, number]>;
  value?: (count: number) => string;
}) {
  const maximum = Math.max(...rows.map((row) => row[1]), 1);
  if (!rows.length) return <p className="empty">No matching measurements.</p>;
  return (
    <div className="bars">
      {rows.slice(0, 10).map(([label, count]) => (
        <div className="bar" key={label}>
          <span title={label}>{label}</span>
          <div className="track" aria-hidden="true">
            <div className="fill" style={{ width: `${(100 * count) / maximum}%` }} />
          </div>
          <b>{value(count)}</b>
        </div>
      ))}
    </div>
  );
}

function Section({
  children,
  title,
  note,
}: {
  children: React.ReactNode;
  title: string;
  note?: string;
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
        {note ? <span>{note}</span> : null}
      </div>
      {children}
    </section>
  );
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
  slice,
}: {
  data: DashboardDatasetResponse;
  slice: DashboardSlice;
}) {
  const calculations = useMemo(() => createDashboardCalculations(data), [data]);
  const semanticCalls = calculations.semanticTokenCalls(slice);
  const metrics = calculations.metrics({ ...slice, calls: semanticCalls });
  const closed = slice.turns.filter(
    (turn) => turn.status === "completed" || turn.status === "aborted",
  );
  const activity = groupSum(
    semanticCalls,
    (call) => calculations.day(call.timestamp),
    (call) => call.total_tokens,
  );
  const tools = groupCount(slice.tools, (call) => call.tool ?? "unknown");
  const models = groupSum(
    semanticCalls,
    (call) => call.model || "unknown",
    (call) => call.total_tokens,
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

  return (
    <>
      <section className="metric-grid" aria-label="Headline metrics">
        <Metric
          label="Closed turns"
          value={number(metrics.completed + metrics.aborted)}
        />
        <Metric label="Active days" value={number(metrics.activeDays)} />
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
        <Metric label="Turn rate" value={`${metrics.throughput.toFixed(1)}/h`} />
        <Metric
          label="Context pressure p95"
          value={formatPercent(metrics.pressureP95)}
        />
      </section>
      <div className="panel-grid">
        <Section title="Activity" note="tokens by UTC day">
          <Bars rows={activity} value={short} />
        </Section>
        <Section title="Tools" note="names only">
          <Bars rows={tools} />
        </Section>
        <Section title="Models" note="provider-reported tokens">
          <Bars rows={models} value={short} />
        </Section>
        <Section title="Turn performance" note="p50 / p75 / p95">
          <div className="mini-grid">
            <Metric label="Duration p50" value={formatDuration(metrics.durationP50)} />
            <Metric label="Duration p95" value={formatDuration(metrics.durationP95)} />
            <Metric label="TTFT p50" value={formatDuration(metrics.ttftP50)} />
            <Metric label="TTFT p95" value={formatDuration(metrics.ttftP95)} />
          </div>
        </Section>
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
        <Section title="Turn outcomes" note="technical status, not task quality">
          <Bars rows={outcomes} />
        </Section>
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
        <Section title="Technical work items" note="content-free intervals">
          <Bars rows={workKinds} value={formatDuration} />
        </Section>
        <Cohorts calculations={calculations} slice={slice} />
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
      </div>
    </>
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

  return (
    <main className="dashboard-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Local-first AI CLI observability</p>
          <h1>CLI Consumption</h1>
          <p>
            Activity, responsiveness, token composition, and workflows — never prompts,
            responses, or tool arguments.
          </p>
        </div>
        <div className="hero-actions">
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
          <>
            <MetricsView data={data} slice={slice} />
            <ConversationExplorer query={query} />
          </>
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
