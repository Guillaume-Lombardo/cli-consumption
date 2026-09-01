import "server-only";

import { readBoundedBytes, readBoundedJsonObject } from "./body";
import { dashboardServerConfig, type DashboardServerConfig } from "./config";
import { safeUpstreamCode } from "./reporting";
import { sameOrigin, verifySessionToken } from "./session";

const REQUEST_BYTES = 64 * 1024;
const RESPONSE_BYTES = 128 * 1024 * 1024;
const EXPORT_TIMEOUT_MS = 65_000;
const DOWNLOAD_NAME = "cli-consumption-dashboard.html";

function json(detail: string, status: number): Response {
  return Response.json(
    { detail },
    { status, headers: { "Cache-Control": "no-store", Pragma: "no-cache" } },
  );
}

/** Create one bounded offline export without exposing collector credentials. */
export async function proxyOfflineExport(
  request: Request,
  sessionToken: string | undefined,
): Promise<Response> {
  let config: DashboardServerConfig;
  try {
    config = dashboardServerConfig();
  } catch {
    return json("reporting_unavailable", 503);
  }
  if (
    !sameOrigin(request, config.dashboardOrigin) ||
    !verifySessionToken(sessionToken, config.sessionSecret)
  ) {
    return json("session_expired", 401);
  }
  if (config.exportToken === null) return json("export_unavailable", 503);

  const parsed = await readBoundedJsonObject(request, REQUEST_BYTES);
  if (parsed.status === "too_large") return json("invalid_reporting_request", 413);
  if (parsed.status === "invalid") return json("invalid_reporting_request", 400);

  let upstream: Response;
  try {
    upstream = await fetch(new URL("/api/v1/reporting/export", config.apiUrl), {
      body: parsed.bytes,
      cache: "no-store",
      headers: {
        Accept: "text/html",
        Authorization: `Bearer ${config.exportToken}`,
        "Content-Type": "application/json",
      },
      method: "POST",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(EXPORT_TIMEOUT_MS)]),
    });
  } catch {
    return json("reporting_unavailable", 502);
  }

  const contentLength = upstream.headers.get("content-length");
  if (contentLength !== null) {
    if (!/^\d+$/.test(contentLength)) {
      await upstream.body?.cancel().catch(() => undefined);
      return json("reporting_unavailable", 502);
    }
    const declaredResponse = Number(contentLength);
    if (!Number.isSafeInteger(declaredResponse) || declaredResponse > RESPONSE_BYTES) {
      await upstream.body?.cancel().catch(() => undefined);
      return json("reporting_response_too_large", 502);
    }
  }
  const response = await readBoundedBytes(upstream.body, RESPONSE_BYTES);
  if (response.status !== "ok") return json("reporting_response_too_large", 502);
  if (!upstream.ok) {
    let error: unknown;
    try {
      error = JSON.parse(Buffer.from(response.bytes).toString("utf8"));
    } catch {
      error = null;
    }
    const status =
      upstream.status >= 400 && upstream.status < 500 ? upstream.status : 502;
    return json(safeUpstreamCode(error), status);
  }
  if (!upstream.headers.get("content-type")?.startsWith("text/html")) {
    return json("reporting_unavailable", 502);
  }

  return new Response(response.bytes, {
    headers: {
      "Cache-Control": "no-store, private",
      "Content-Disposition": `attachment; filename="${DOWNLOAD_NAME}"`,
      "Content-Type": "text/html; charset=utf-8",
      Pragma: "no-cache",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
