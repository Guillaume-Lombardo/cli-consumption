import { redirect } from "next/navigation";

import { LoginForm } from "./login-form";
import { hasDashboardSession } from "../../server/session";

export const dynamic = "force-dynamic";

export default async function LoginPage() {
  if (await hasDashboardSession()) redirect("/dashboard");
  return (
    <main className="login-shell">
      <section className="login-card" aria-labelledby="login-title">
        <p className="eyebrow">Private analytics</p>
        <h1 id="login-title">CLI Consumption</h1>
        <p>
          Sign in to view metadata-only reporting. Provider content is never displayed.
        </p>
        <LoginForm />
      </section>
    </main>
  );
}
