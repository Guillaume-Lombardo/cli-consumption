import {
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  useState,
} from "react";

type ActivityMetric = "tokens" | "turns" | "conversations" | "duration";
interface ChartCatalogData {
  days: Array<{
    date: string;
    observed: boolean;
    values: Partial<Record<ActivityMetric, number>>;
  }>;
  availableMetrics: ActivityMetric[];
  currentStreak: number;
  longestStreak: number;
  dailyPeakTokens: number | null;
  tokenComposition: Array<[string, number]>;
  tokenSeries: Array<{
    date: string;
    total: number;
    providers: Record<string, number>;
    models: Record<string, number>;
  }>;
  availableBreakdowns: Array<"provider" | "model">;
  rankings: {
    models: Array<[string, number]>;
    providers: Array<[string, number]>;
    projects: Array<[string, number]>;
    tools: Array<[string, number]>;
  };
}

const ACTIVITY_LABELS: Record<ActivityMetric, string> = {
  tokens: "Tokens",
  turns: "Turns",
  conversations: "Conversations",
  duration: "Turn duration",
};

function activityValue(metric: ActivityMetric, value: number) {
  return metric === "duration"
    ? `${Math.round(value / 60000).toLocaleString("en")} min`
    : `${value.toLocaleString("en")} ${ACTIVITY_LABELS[metric].toLowerCase()}`;
}

/** Shared chart catalog renderer: the same data and semantics are used online/offline. */
export function ActivityCatalog({ catalog }: { catalog: ChartCatalogData }) {
  const [selected, setSelected] = useState<ActivityMetric>(
    catalog.availableMetrics[0] ?? "turns",
  );
  const [focusIndex, setFocusIndex] = useState(() =>
    Math.max(
      0,
      catalog.days.findLastIndex((row) => row.observed),
    ),
  );
  const [breakdown, setBreakdown] = useState<"overall" | "provider" | "model">(
    "overall",
  );
  const metric = catalog.availableMetrics.includes(selected)
    ? selected
    : catalog.availableMetrics[0];
  const maximum = metric
    ? Math.max(...catalog.days.map((row) => row.values[metric] ?? 0), 1)
    : 1;
  const breakdownTotals = new Map<string, number>();
  if (breakdown !== "overall") {
    for (const point of catalog.tokenSeries) {
      const values = breakdown === "provider" ? point.providers : point.models;
      for (const [label, value] of Object.entries(values))
        breakdownTotals.set(label, (breakdownTotals.get(label) ?? 0) + value);
    }
  }
  const leadingLabels = [...breakdownTotals]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, 5)
    .map(([label]) => label);
  const breakdownLabels =
    breakdown === "overall"
      ? ["Overall"]
      : [...leadingLabels, ...(breakdownTotals.size > 5 ? ["Other"] : [])];
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const delta =
      event.key === "ArrowRight"
        ? 7
        : event.key === "ArrowLeft"
          ? -7
          : event.key === "ArrowDown"
            ? 1
            : event.key === "ArrowUp"
              ? -1
              : 0;
    if (!delta) return;
    event.preventDefault();
    const next = index + delta;
    const target =
      event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[
        next
      ];
    if (target) setFocusIndex(next);
    target?.focus();
  };
  return (
    <div className="chart-catalog">
      <div className="activity-summary">
        <label>
          Calendar metric{" "}
          <select
            aria-label="Calendar metric"
            disabled={!metric}
            value={metric ?? ""}
            onChange={(event) => setSelected(event.target.value as ActivityMetric)}
          >
            {catalog.availableMetrics.map((option) => (
              <option key={option} value={option}>
                {ACTIVITY_LABELS[option]}
              </option>
            ))}
          </select>
        </label>
        <span>
          <b>{catalog.currentStreak}</b> current streak
        </span>
        <span>
          <b>{catalog.longestStreak}</b> longest streak
        </span>
      </div>
      <section
        className="calendar-scroll"
        aria-label="Scrollable 52-week UTC activity calendar"
      >
        <div className="activity-axis" aria-hidden="true">
          <span>Sun</span>
          <span>Tue</span>
          <span>Thu</span>
          <span>Sat</span>
        </div>
        <div className="activity-months" aria-hidden="true">
          {catalog.days
            .filter((row) => row.date.endsWith("-01"))
            .map((row) => (
              <span key={row.date}>
                {new Date(`${row.date}T00:00:00Z`).toLocaleString("en", {
                  month: "short",
                  timeZone: "UTC",
                })}
              </span>
            ))}
        </div>
        {!metric ? (
          <p className="empty">
            No attributable daily measurements in this selection. Aggregate totals
            remain available below.
          </p>
        ) : (
          <fieldset className="activity-calendar">
            <legend className="sr-only">{`${ACTIVITY_LABELS[metric]} activity, 52 UTC weeks`}</legend>
            {catalog.days.map((row, index) => {
              const value = row.values[metric] ?? 0;
              const level = row.observed
                ? Math.min(4, Math.ceil((4 * value) / maximum))
                : -1;
              const description = row.observed
                ? activityValue(metric, value)
                : "not in selected range";
              return (
                <button
                  aria-label={`${row.date}: ${description}`}
                  className="activity-cell"
                  data-level={level}
                  data-tooltip={`${row.date}: ${description}`}
                  key={row.date}
                  onKeyDown={(event) => onKeyDown(event, index)}
                  onFocus={() => setFocusIndex(index)}
                  tabIndex={index === focusIndex ? 0 : -1}
                  title={`${row.date}: ${description}`}
                  type="button"
                />
              );
            })}
          </fieldset>
        )}
      </section>
      <div className="activity-legend">
        <span className="sr-only">Calendar scale</span>
        <span>Missing</span>
        <i data-level="-1" />
        <span>Zero</span>
        <i data-level="0" />
        <span>Low</span>
        <i data-level="2" />
        <span>High</span>
        <i data-level="4" />
      </div>
      <details>
        <summary>Daily values table</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">UTC date</th>
                <th scope="col">{metric ? ACTIVITY_LABELS[metric] : "Unavailable"}</th>
              </tr>
            </thead>
            <tbody>
              {metric
                ? catalog.days
                    .filter((row) => row.observed)
                    .map((row) => (
                      <tr key={row.date}>
                        <th scope="row">{row.date}</th>
                        <td>{activityValue(metric, row.values[metric] ?? 0)}</td>
                      </tr>
                    ))
                : null}
            </tbody>
          </table>
        </div>
      </details>
      {catalog.tokenSeries.length ? (
        <div className="token-series-panel">
          <label>
            Token series{" "}
            <select
              aria-label="Token series breakdown"
              value={breakdown}
              onChange={(event) =>
                setBreakdown(event.target.value as "overall" | "provider" | "model")
              }
            >
              <option value="overall">Overall</option>
              {catalog.availableBreakdowns.map((option) => (
                <option key={option} value={option}>
                  By {option}
                </option>
              ))}
            </select>
          </label>
          <div
            className="token-series"
            role="img"
            aria-label={`Daily provider-reported tokens, ${breakdown}`}
          >
            {catalog.tokenSeries.map((point) => {
              const source = breakdown === "provider" ? point.providers : point.models;
              const groups = breakdownLabels.map(
                (label) =>
                  [
                    label,
                    label === "Overall"
                      ? point.total
                      : label === "Other"
                        ? Object.entries(source)
                            .filter(([name]) => !leadingLabels.includes(name))
                            .reduce((sum, [, value]) => sum + value, 0)
                        : (source[label] ?? 0),
                  ] as [string, number],
              );
              const peak = Math.max(...catalog.tokenSeries.map((row) => row.total), 1);
              return (
                <div
                  className="token-series-day"
                  key={point.date}
                  title={`${point.date}: ${point.total.toLocaleString("en")} tokens`}
                  style={{ height: `${Math.max(1, (100 * point.total) / peak)}%` }}
                >
                  {groups.map(([label, value], index) => (
                    <i
                      className={`series-color-${index % 6}`}
                      key={label}
                      style={{ flexGrow: value }}
                      title={`${label}: ${value.toLocaleString("en")}`}
                    />
                  ))}
                </div>
              );
            })}
          </div>
          <div className="activity-legend">
            {breakdownLabels.map((label, index) => (
              <span key={label}>
                <i className={`series-color-${index % 6}`} />
                {label}
              </span>
            ))}
          </div>
          <details>
            <summary>Token series table</summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">UTC date</th>
                    {breakdownLabels.map((label) => (
                      <th key={label} scope="col">
                        {label} tokens
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {catalog.tokenSeries.map((point) => {
                    const source =
                      breakdown === "provider" ? point.providers : point.models;
                    return (
                      <tr key={point.date}>
                        <th scope="row">{point.date}</th>
                        {breakdownLabels.map((label) => (
                          <td key={label}>
                            {(label === "Overall"
                              ? point.total
                              : label === "Other"
                                ? Object.entries(source)
                                    .filter(([name]) => !leadingLabels.includes(name))
                                    .reduce((sum, [, value]) => sum + value, 0)
                                : (source[label] ?? 0)
                            ).toLocaleString("en")}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      ) : null}
      <div
        className="token-stack"
        role="img"
        aria-label="Provider-reported token composition"
      >
        {catalog.tokenComposition.map(([label, value], index) => (
          <div
            key={label}
            className={`token-segment token-segment-${index}`}
            style={{ flexGrow: value }}
            title={`${label}: ${value.toLocaleString("en")}`}
          >
            <span>{value ? label : ""}</span>
          </div>
        ))}
      </div>
      <div className="activity-legend">
        {catalog.tokenComposition.map(([label, value]) => (
          <span key={label}>
            <b>{label}</b> {value.toLocaleString("en")}
          </span>
        ))}
      </div>
      <div className="catalog-rankings">
        <div>
          <h3>Provider ranking</h3>
          <Bars rows={catalog.rankings.providers} />
        </div>
        <div>
          <h3>Project ranking</h3>
          <Bars rows={catalog.rankings.projects} />
        </div>
      </div>
    </div>
  );
}

export interface LayoutWidget {
  id: string;
  type: string;
  position: { x: number; y: number };
  size: { width: number; height: number };
}

export function DashboardLayoutGrid({
  renderWidget,
  widgets,
}: {
  renderWidget: (type: string) => ReactNode;
  widgets: readonly LayoutWidget[];
}) {
  return (
    <div className="dashboard-layout-grid" data-layout-widget-count={widgets.length}>
      {widgets.map((widget) => (
        <div
          className="dashboard-layout-widget"
          data-position-x={widget.position.x}
          data-position-y={widget.position.y}
          data-size-height={widget.size.height}
          data-size-width={widget.size.width}
          data-widget-id={widget.id}
          data-widget-type={widget.type}
          key={widget.id}
          style={
            {
              "--layout-column": widget.position.x + 1,
              "--layout-row": widget.position.y + 1,
              "--layout-width": widget.size.width,
              "--layout-height": widget.size.height,
            } as CSSProperties
          }
        >
          {renderWidget(widget.type)}
        </div>
      ))}
    </div>
  );
}

export function Metric({
  help,
  label,
  value,
}: {
  help?: string;
  label: string;
  value: string;
}) {
  return (
    <article className="metric-card card">
      <span>{label}</span>
      <strong>{value}</strong>
      {help ? <p>{help}</p> : null}
    </article>
  );
}

export function Bars({
  rows,
  value = (count) => Intl.NumberFormat("en").format(count),
}: {
  rows: Array<[string, number]>;
  value?: (count: number) => string;
}) {
  const maximum = Math.max(...rows.map((row) => row[1]), 1);
  if (!rows.length) return <p className="empty">No matching measurements.</p>;
  return (
    <ul className="bars">
      {rows.slice(0, 10).map(([label, count]) => (
        <li className="bar" key={label} aria-label={`${label}: ${value(count)}`}>
          <span title={label}>{label}</span>
          <div className="track" aria-hidden="true">
            <div className="fill" style={{ width: `${(100 * count) / maximum}%` }} />
          </div>
          <b>{value(count)}</b>
        </li>
      ))}
    </ul>
  );
}

export function Section({
  children,
  note,
  title,
}: {
  children: ReactNode;
  note?: string;
  title: string;
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
