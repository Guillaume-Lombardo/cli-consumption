import {
  createDashboardCalculations,
  type DashboardSlice,
} from "@cli-consumption/analytics";
import {
  assertDashboardDatasetV1,
  dashboardLayoutComposition,
  type DashboardDatasetV1,
  type DashboardWidgetType,
} from "@cli-consumption/contracts";
import { formatDuration, formatPercent } from "@cli-consumption/ui";
import { Bars, DashboardLayoutGrid, Metric, Section } from "@cli-consumption/ui/react";
import { type ReactNode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

declare global {
  var __CLI_CONSUMPTION_DATASET__: unknown;
  var __CLI_CONSUMPTION_LAYOUT__: unknown;
}
type FilterKey = "provider" | "machine" | "project" | "model";
type Filters = Record<FilterKey, string>;
type CohortDimension =
  | "project"
  | "model"
  | "effort"
  | "mode"
  | "delegation"
  | "compaction";

const dataset = globalThis.__CLI_CONSUMPTION_DATASET__;
assertDashboardDatasetV1(dataset);
const layout = dashboardLayoutComposition(globalThis.__CLI_CONSUMPTION_LAYOUT__);
const number = (value: number | null | undefined) =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : Intl.NumberFormat("en").format(Math.round(value));
const short = (value: number | null | undefined) =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : Intl.NumberFormat("en", {
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(value);

function group<T>(
  rows: readonly T[],
  label: (row: T) => string,
  value: (row: T) => number = () => 1,
) {
  const grouped = new Map<string, number>();
  for (const row of rows) {
    const key = label(row) || "unknown";
    grouped.set(key, (grouped.get(key) ?? 0) + value(row));
  }
  return [...grouped].sort((left, right) => right[1] - left[1]);
}

function options(data: DashboardDatasetV1, key: FilterKey) {
  const values =
    key === "model"
      ? data.conversations.flatMap((conversation) => conversation.models)
      : data.conversations.map((conversation) => conversation[key]);
  return [...new Set(values.filter(Boolean))].sort();
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
            <thead>
              <tr>
                <th>Cohort</th>
                <th>Closed turns</th>
                <th>Median duration</th>
                <th>Median tokens</th>
                <th>Tools / turn</th>
                <th>Context p95</th>
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
                  <td>{row.toolsPerTurn.toFixed(1)}</td>
                  <td>{formatPercent(row.pressureP95)}</td>
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

function Dashboard({ data }: { data: DashboardDatasetV1 }) {
  const calculations = useMemo(() => createDashboardCalculations(data), [data]);
  const [period, setPeriod] = useState("all");
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [filters, setFilters] = useState<Filters>({
    machine: "",
    model: "",
    project: "",
    provider: "",
  });
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark",
  );
  const [selectedConversation, setSelectedConversation] = useState<number | null>(null);
  document.documentElement.dataset.theme = theme;

  const range = calculations.rangeFor(period, custom);
  const slice = calculations.selectSlice({ ...filters, range });
  const metrics = calculations.metrics(slice);
  const calls = calculations.semanticTokenCalls(slice);
  const activity = group(
    calls,
    (call) => calculations.day(call.timestamp),
    (call) => call.total_tokens,
  );
  const modelRows = group(
    calls,
    (call) => call.model,
    (call) => call.total_tokens,
  );
  const toolRows = group(slice.tools, (tool) => tool.tool ?? "unknown");
  const outcomes = group(slice.turns, (turn) => turn.status);
  const work = group(
    slice.work,
    (item) => item.kind,
    (item) => item.durationMs ?? 0,
  );
  const selected = slice.conversations.find(
    (conversation) => conversation.key === selectedConversation,
  );
  const malformed = data.ingestionRuns
    .filter(
      (run) =>
        (!filters.provider || run.provider === filters.provider) &&
        calculations.inRange(run.ingestedAt, range),
    )
    .reduce((sum, run) => sum + run.malformed, 0);
  const durationCoverage = calculations.ratio(
    slice.turns.filter((turn) => turn.durationMs !== null).length,
    slice.turns.length,
  );
  const ttftCoverage = calculations.ratio(
    slice.turns.filter((turn) => turn.ttftMs !== null).length,
    slice.turns.length,
  );
  const pressures = slice.contexts
    .map((sample) => (100 * sample.inputTokens) / sample.contextWindowTokens)
    .filter(Number.isFinite);
  const compacted = new Set(slice.compactions.map((item) => item.conversationKey)).size;
  const delegated = new Set(slice.subagents.map((item) => item.conversationKey)).size;

  function updateFilter(key: FilterKey, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
    setSelectedConversation(null);
  }

  const widgetContent: Record<DashboardWidgetType, ReactNode> = {
    "headline-metrics": (
      <section className="metric-grid" id="cards" aria-label="Headline metrics">
        <Metric
          label="Closed turns"
          value={number(metrics.completed + metrics.aborted)}
        />
        <Metric label="Active days" value={number(metrics.activeDays)} />
        <Metric
          label="Total tokens"
          value={short(metrics.tokens)}
          help="Local provider counters; not billing data."
        />
        <Metric label="Median tokens / turn" value={short(metrics.tokensPerTurn)} />
        <Metric label="Median TTFT" value={formatDuration(metrics.ttftP50)} />
        <Metric label="Median duration" value={formatDuration(metrics.durationP50)} />
        <Metric label="Turn rate" value={`${metrics.throughput.toFixed(1)}/h`} />
        <Metric
          label="Context pressure p95"
          value={formatPercent(metrics.pressureP95)}
        />
        <Metric
          label={data.meta.shareSafe ? "Summed turn time" : "Active time"}
          value={formatDuration(metrics.activeMs)}
        />
      </section>
    ),
    activity: (
      <Section title="Activity" note="tokens by UTC day">
        <Bars rows={activity} value={short} />
      </Section>
    ),
    tools: (
      <Section title="Tools" note="names only">
        <Bars rows={toolRows} />
      </Section>
    ),
    models: (
      <Section title="Models" note="provider-reported tokens">
        <Bars rows={modelRows} value={short} />
      </Section>
    ),
    "turn-performance": (
      <Section title="Turn performance" note="p50 / p95">
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
            value={number(calculations.maxConcurrent(slice.turns))}
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
            value={formatPercent(calculations.percentile(pressures, 0.5))}
          />
          <Metric
            label="Pressure p95"
            value={formatPercent(calculations.percentile(pressures, 0.95))}
          />
          <Metric label="Context samples" value={number(slice.contexts.length)} />
          <Metric label="Turn configurations" value={number(slice.settings.length)} />
        </div>
      </Section>
    ),
    "technical-work-items": (
      <Section title="Technical work items" note="content-free intervals">
        <Bars rows={work} value={formatDuration} />
      </Section>
    ),
    cohorts: <Cohorts calculations={calculations} slice={slice} />,
    "data-quality": (
      <Section title="Data quality" note="coverage and ingestion health">
        <div className="mini-grid">
          <Metric label="Duration coverage" value={formatPercent(durationCoverage)} />
          <Metric label="Malformed records" value={number(malformed)} />
          <Metric label="TTFT coverage" value={formatPercent(ttftCoverage)} />
          <Metric
            label="Unattributed token share"
            value={formatPercent(
              calculations.ratio(
                calculations.total(calls, "unattributed_tokens"),
                calculations.total(calls, "total_tokens"),
              ),
            )}
          />
        </div>
      </Section>
    ),
    "conversation-explorer": (
      <Section title="Conversation explorer" note="complete related rows">
        <span className="muted" id="conversationCount">
          {slice.conversations.length} conversations
        </span>
        <section
          className="table-scroll"
          aria-label="Scrollable conversations"
          // biome-ignore lint/a11y/noNoninteractiveTabindex: overflow regions need keyboard access in Safari.
          tabIndex={0}
        >
          <table id="table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Provider</th>
                <th>Machine</th>
                <th>Project</th>
                <th>Models</th>
                <th>Turns</th>
                <th>Tokens</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {slice.conversations.map((conversation) => (
                <tr key={conversation.key}>
                  <td>{conversation.startedAt ?? "Unknown"}</td>
                  <td>{conversation.provider}</td>
                  <td>{conversation.machine}</td>
                  <td>{conversation.project}</td>
                  <td>{conversation.models.join(", ") || "Unknown"}</td>
                  <td>{number(conversation.turns)}</td>
                  <td>{short(conversation.total_tokens)}</td>
                  <td>
                    <button
                      className="secondary inspect"
                      type="button"
                      onClick={() => setSelectedConversation(conversation.key)}
                    >
                      Inspect
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        {selected ? (
          <section className="detail-card" aria-label="Conversation detail">
            <h3>{selected.project}</h3>
            <p>
              {selected.provider} · {selected.machine} ·{" "}
              {selected.models.join(", ") || "Unknown model"}
            </p>
            <div className="mini-grid">
              <Metric label="Turns" value={number(selected.turns)} />
              <Metric label="Model calls" value={number(selected.modelCalls)} />
              <Metric label="Tool calls" value={number(selected.toolCalls)} />
              <Metric label="Compactions" value={number(selected.compactions)} />
            </div>
          </section>
        ) : null}
      </Section>
    ),
  };

  return (
    <main className="dashboard-shell">
      <header className="hero">
        <div className="hero-intro">
          <h1>CLI Consumption</h1>
          <div className="hero-copy">
            <p className="eyebrow">Portable local-first report</p>
            <p>
              Activity, responsiveness, token composition, and workflows — never
              prompts, responses, or tool arguments.
            </p>
            {data.meta.shareSafe ? (
              <strong className="privacy-badge" id="privacyBadge">
                Share-safe dashboard
              </strong>
            ) : null}
          </div>
        </div>
        <button
          className="secondary"
          id="themeToggle"
          type="button"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
      </header>
      <section className="filter-panel" aria-label="Dashboard filters">
        <label>
          Period
          <select
            id="period"
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
          >
            <option value="7">Latest 7 days</option>
            <option value="30">Latest 30 days</option>
            <option value="90">Latest 90 days</option>
            <option value="all">All history</option>
            <option value="custom">Custom range</option>
          </select>
        </label>
        {(["provider", "machine", "project", "model"] as const).map((key) => (
          <label key={key}>
            {key.slice(0, 1).toUpperCase() + key.slice(1)}
            <select
              id={key}
              value={filters[key]}
              onChange={(event) => updateFilter(key, event.target.value)}
            >
              <option value="">All</option>
              {options(data, key).map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
        ))}
      </section>
      <section
        className={`custom-dates ${period === "custom" ? "visible" : ""}`}
        id="customDates"
      >
        <label>
          From
          <input
            type="date"
            value={custom.from}
            onChange={(event) => setCustom({ ...custom, from: event.target.value })}
          />
        </label>
        <label>
          To
          <input
            type="date"
            value={custom.to}
            onChange={(event) => setCustom({ ...custom, to: event.target.value })}
          />
        </label>
      </section>
      <DashboardLayoutGrid
        widgets={layout}
        renderWidget={(type) => widgetContent[type as DashboardWidgetType]}
      />
      <details>
        <summary>Metric definitions and privacy notes</summary>
        <p>
          Token events are local usage metadata, not billing data. Detailed reports
          contain operational labels and timestamps. Share-safe reports pseudonymize
          labels, group tools, and round timestamps.
        </p>
      </details>
      <footer>
        Generated as one self-contained file. It performs no network request.
      </footer>
    </main>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("offline_dashboard_root_missing");
createRoot(root).render(<Dashboard data={dataset} />);
