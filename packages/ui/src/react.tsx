import type { CSSProperties, ReactNode } from "react";

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
