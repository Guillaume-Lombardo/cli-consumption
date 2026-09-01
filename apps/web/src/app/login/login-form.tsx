"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

export function LoginForm() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    const form = new FormData(event.currentTarget);
    let response: Response;
    try {
      response = await fetch("/api/session", {
        body: JSON.stringify({ password: form.get("password") }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
    } catch {
      setError("Sign-in failed. Check the dashboard credential and try again.");
      return;
    } finally {
      setPending(false);
    }
    if (!response.ok) {
      setError("Sign-in failed. Check the dashboard credential and try again.");
      return;
    }
    router.replace("/dashboard");
    router.refresh();
  }

  return (
    <form onSubmit={submit} className="login-form">
      <label htmlFor="password">Dashboard password</label>
      <input
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        required
      />
      <button type="submit" disabled={pending}>
        {pending ? "Signing in…" : "Sign in"}
      </button>
      <p className="form-error" role="alert" aria-live="polite">
        {error}
      </p>
    </form>
  );
}
