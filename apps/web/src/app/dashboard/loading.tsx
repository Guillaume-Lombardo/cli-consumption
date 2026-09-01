export default function DashboardLoading() {
  return (
    <main className="dashboard-shell" aria-busy="true">
      <div className="skeleton hero-skeleton" />
      <div className="metric-grid">
        {["turns", "days", "tokens", "ttft", "duration", "pressure"].map((name) => (
          <div className="skeleton metric-skeleton" key={name} />
        ))}
      </div>
    </main>
  );
}
