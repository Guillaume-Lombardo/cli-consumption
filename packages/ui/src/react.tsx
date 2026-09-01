import type { ReactNode } from "react";

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
