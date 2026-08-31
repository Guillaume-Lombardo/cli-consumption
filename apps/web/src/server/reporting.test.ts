import { afterEach, describe, expect, it, vi } from "vitest";

import { createSessionToken } from "./session";
import { proxyReportingRequest, safeUpstreamCode } from "./reporting";

const CANARY = "CANARY_DO_NOT_EXPOSE_7d9f";
const ORIGIN = "https://dashboard.example";
const SESSION_SECRET = "session-secret-with-at-least-thirty-two-bytes";

function configure() {
  process.env.CLI_CONSUMPTION_API_URL = "https://collector.example";
  process.env.CLI_CONSUMPTION_DASHBOARD_ORIGIN = ORIGIN;
  process.env.CLI_CONSUMPTION_DASHBOARD_PASSWORD = "dashboard password value";
  process.env.CLI_CONSUMPTION_READ_TOKEN = CANARY;
  process.env.CLI_CONSUMPTION_SESSION_SECRET = SESSION_SECRET;
}

function request(body = '{"version":1}') {
  return new Request(`${ORIGIN}/api/reporting/dashboard`, {
    body,
    headers: { "Content-Type": "application/json", Origin: ORIGIN },
    method: "POST",
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  for (const name of [
    "CLI_CONSUMPTION_API_URL",
    "CLI_CONSUMPTION_DASHBOARD_ORIGIN",
    "CLI_CONSUMPTION_DASHBOARD_PASSWORD",
    "CLI_CONSUMPTION_READ_TOKEN",
    "CLI_CONSUMPTION_SESSION_SECRET",
  ]) {
    delete process.env[name];
  }
});

describe("reporting BFF", () => {
  it("keeps the upstream credential server-side and disables caching", async () => {
    configure();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (_url, init) => {
        expect(new Headers(init?.headers).get("Authorization")).toBe(
          `Bearer ${CANARY}`,
        );
        return Response.json({ contractVersion: 1, meta: { shareSafe: false } });
      });
    const response = await proxyReportingRequest(
      request(),
      "dashboard",
      createSessionToken(SESSION_SECRET),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await response.text()).not.toContain(CANARY);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("reduces malformed upstream errors to fixed codes without logging secrets", async () => {
    configure();
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ detail: `driver failed with ${CANARY}` }, { status: 500 }),
    );
    const response = await proxyReportingRequest(
      request(),
      "dashboard",
      createSessionToken(SESSION_SECRET),
    );
    const text = await response.text();

    expect(response.status).toBe(502);
    expect(text).toBe('{"detail":"reporting_unavailable"}');
    expect(text).not.toContain(CANARY);
    expect(log).not.toHaveBeenCalled();
  });

  it("rejects cross-origin, expired, oversized, and malformed bodies before fetch", async () => {
    configure();
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const token = createSessionToken(SESSION_SECRET);
    const crossOrigin = new Request(request(), {
      headers: { Origin: "https://other.example" },
    });
    expect((await proxyReportingRequest(crossOrigin, "dashboard", token)).status).toBe(
      401,
    );
    expect(
      (await proxyReportingRequest(request(), "dashboard", "invalid")).status,
    ).toBe(401);
    expect(
      (await proxyReportingRequest(request("not-json"), "dashboard", token)).status,
    ).toBe(400);
    expect(
      (
        await proxyReportingRequest(
          request(`{"value":"${"x".repeat(65_536)}"}`),
          "dashboard",
          token,
        )
      ).status,
    ).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allowlists only documented upstream error codes", () => {
    expect(safeUpstreamCode({ detail: "pagination_expired" })).toBe(
      "pagination_expired",
    );
    expect(safeUpstreamCode({ detail: CANARY })).toBe("reporting_unavailable");
    expect(safeUpstreamCode(CANARY)).toBe("reporting_unavailable");
  });
});
