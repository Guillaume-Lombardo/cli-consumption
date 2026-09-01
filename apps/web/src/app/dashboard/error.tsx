"use client";

export default function DashboardError({ reset }: { reset: () => void }) {
  return (
    <main className="login-shell">
      <section className="login-card" role="alert">
        <p className="eyebrow">Dashboard unavailable</p>
        <h1>We could not load reporting</h1>
        <p>No upstream details were exposed. Try the bounded request again.</p>
        <button type="button" onClick={reset}>
          Retry
        </button>
      </section>
    </main>
  );
}
