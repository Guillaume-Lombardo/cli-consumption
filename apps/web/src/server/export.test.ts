import { afterEach, describe, expect, it, vi } from "vitest";

import { proxyOfflineExport } from "./export";
import { createSessionToken } from "./session";

const CANARY = "CANARY_EXPORT_TOKEN_09c1";
const ORIGIN = "https://dashboard.example";
const SESSION_SECRET = "session-secret-with-at-least-thirty-two-bytes";

function configure(exportToken: string | null = CANARY) {
  process.env.CLI_CONSUMPTION_API_URL = "https://collector.example";
  process.env.CLI_CONSUMPTION_DASHBOARD_ORIGIN = ORIGIN;
  process.env.CLI_CONSUMPTION_DASHBOARD_PASSWORD = "dashboard password value";
  process.env.CLI_CONSUMPTION_READ_TOKEN = "read-token";
  process.env.CLI_CONSUMPTION_SESSION_SECRET = SESSION_SECRET;
  if (exportToken === null) delete process.env.CLI_CONSUMPTION_EXPORT_TOKEN;
  else process.env.CLI_CONSUMPTION_EXPORT_TOKEN = exportToken;
}

function request(body = '{"version":1,"profile":"detailed"}') {
  return new Request(`${ORIGIN}/api/reporting/export`, {
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
    "CLI_CONSUMPTION_EXPORT_TOKEN",
    "CLI_CONSUMPTION_READ_TOKEN",
    "CLI_CONSUMPTION_SESSION_SECRET",
  ]) {
    delete process.env[name];
  }
});

describe("offline export BFF", () => {
  it("forwards the exact body with a server-only credential and download headers", async () => {
    configure();
    const body =
      '{"version":1,"profile":"share-safe","filters":{"projects":["private"]}}';
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        expect(String(input)).toBe("https://collector.example/api/v1/reporting/export");
        expect(Buffer.from(init?.body as Uint8Array).toString()).toBe(body);
        expect(new Headers(init?.headers).get("Authorization")).toBe(
          `Bearer ${CANARY}`,
        );
        return new Response("<!doctype html><title>Offline</title>", {
          headers: { "Content-Type": "text/html; charset=utf-8" },
        });
      });

    const response = await proxyOfflineExport(
      request(body),
      createSessionToken(SESSION_SECRET),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store, private");
    expect(response.headers.get("Content-Disposition")).toContain(
      "cli-consumption-dashboard.html",
    );
    expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(await response.text()).not.toContain(CANARY);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("fails closed before fetch for invalid sessions, bodies, and configuration", async () => {
    configure();
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const token = createSessionToken(SESSION_SECRET);
    expect((await proxyOfflineExport(request(), "invalid")).status).toBe(401);
    expect((await proxyOfflineExport(request("not-json"), token)).status).toBe(400);
    expect(
      (await proxyOfflineExport(request(`{"value":"${"x".repeat(65_536)}"}`), token))
        .status,
    ).toBe(413);
    configure(null);
    expect((await proxyOfflineExport(request(), token)).status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("bounds the upstream file and suppresses private upstream errors", async () => {
    configure();
    const token = createSessionToken(SESSION_SECRET);
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(null, { headers: { "Content-Length": String(129 * 1024 * 1024) } }),
    );
    const oversized = await proxyOfflineExport(request(), token);
    expect(oversized.status).toBe(502);
    expect(await oversized.json()).toEqual({ detail: "reporting_response_too_large" });

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      Response.json({ detail: `failed ${CANARY}` }, { status: 500 }),
    );
    const failed = await proxyOfflineExport(request(), token);
    expect(failed.status).toBe(502);
    expect(await failed.text()).toBe('{"detail":"reporting_unavailable"}');
  });
});
