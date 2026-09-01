import { redirect } from "next/navigation";

import { ConfigurationUnavailable } from "../configuration-unavailable";
import { LoginForm } from "./login-form";
import { dashboardSessionState } from "../../server/session";

export const dynamic = "force-dynamic";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string | string[] }>;
}) {
  const state = await dashboardSessionState();
  if (state === "authenticated") redirect("/dashboard");
  if (state === "unavailable") return <ConfigurationUnavailable />;
  const { reason } = await searchParams;
  return (
    <main className="login-shell">
      <section className="login-card" aria-labelledby="login-title">
        <p className="eyebrow">Private analytics</p>
        <h1 id="login-title">CLI Consumption</h1>
        <p>
          Sign in to view metadata-only reporting. Provider content is never displayed.
        </p>
        {reason === "session" ? (
          <p className="callout">Your session expired. Sign in again to continue.</p>
        ) : null}
        <LoginForm />
      </section>
    </main>
  );
}
