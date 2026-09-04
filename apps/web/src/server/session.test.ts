import { afterEach, describe, expect, it } from "vitest";

import {
  createSessionToken,
  passwordMatches,
  sameOrigin,
  verifySessionToken,
} from "./session";

const SECRET = "session-secret-with-at-least-thirty-two-bytes";

afterEach(() => {
  delete process.env.CLI_CONSUMPTION_API_URL;
  delete process.env.CLI_CONSUMPTION_DASHBOARD_ORIGIN;
  delete process.env.CLI_CONSUMPTION_DASHBOARD_PASSWORD;
  delete process.env.CLI_CONSUMPTION_READ_TOKEN;
  delete process.env.CLI_CONSUMPTION_LAYOUT_TOKEN;
  delete process.env.CLI_CONSUMPTION_SESSION_SECRET;
});

describe("dashboard sessions", () => {
  it("authenticates a bounded signed token and rejects tampering or expiry", () => {
    const now = Date.UTC(2026, 7, 31);
    const token = createSessionToken(SECRET, now);

    expect(verifySessionToken(token, SECRET, now)).toBe(true);
    expect(verifySessionToken(`${token}x`, SECRET, now)).toBe(false);
    expect(verifySessionToken(token, SECRET, now + 9 * 60 * 60 * 1000)).toBe(false);
    expect(token).not.toContain(SECRET);
  });

  it("compares dashboard passwords without returning either value", () => {
    const password = "correct horse battery staple";
    expect(passwordMatches(password, password)).toBe(true);
    expect(passwordMatches("wrong password", password)).toBe(false);
  });

  it("requires an exact same-origin browser request", () => {
    const request = new Request("https://dashboard.example/api/session", {
      headers: { Origin: "https://dashboard.example" },
    });
    expect(sameOrigin(request, null)).toBe(true);
    expect(sameOrigin(request, "https://dashboard.example")).toBe(true);
    expect(sameOrigin(request, "https://other.example")).toBe(false);
    expect(sameOrigin(new Request(request.url), null)).toBe(false);
  });
});
