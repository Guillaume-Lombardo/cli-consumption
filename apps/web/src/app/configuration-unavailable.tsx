export function ConfigurationUnavailable() {
  return (
    <main className="login-shell">
      <section className="login-card" role="alert">
        <p className="eyebrow">Dashboard unavailable</p>
        <h1>Server configuration is incomplete</h1>
        <p>
          Check the documented dashboard environment variables and restart the service.
        </p>
      </section>
    </main>
  );
}
