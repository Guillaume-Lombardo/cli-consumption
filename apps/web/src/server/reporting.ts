import "server-only";

import { dashboardServerConfig, type DashboardServerConfig } from "./config";
import { readBoundedBytes, readBoundedJsonObject } from "./body";
import { sameOrigin, verifySessionToken } from "./session";

const REQUEST_BYTES = 64 * 1024;
const ROUTES = {
  conversation: { path: "conversation", responseBytes: 16 * 1024 * 1024 },
  conversations: { path: "conversations", responseBytes: 4 * 1024 * 1024 },
  dashboard: { path: "dashboard", responseBytes: 32 * 1024 * 1024 },
  filters: { path: "filters", responseBytes: 2 * 1024 * 1024 },
} as const;

export type ReportingResource = keyof typeof ROUTES;

const SAFE_UPSTREAM_CODES = new Set([
  "conversation_not_found",
  "dashboard_response_limit_exceeded",
  "invalid_reporting_request",
  "pagination_expired",
  "reporting_busy",
  "reporting_limit_exceeded",
  "reporting_timeout",
]);

export function isReportingResource(value: string): value is ReportingResource {
  return Object.hasOwn(ROUTES, value);
}

function json(detail: string, status: number): Response {
  return Response.json(
    { detail },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

export function safeUpstreamCode(value: unknown): string {
  if (typeof value !== "object" || value === null) return "reporting_unavailable";
  const detail = (value as Record<string, unknown>).detail;
  return typeof detail === "string" && SAFE_UPSTREAM_CODES.has(detail)
    ? detail
    : "reporting_unavailable";
}

export async function proxyReportingRequest(
  request: Request,
  resource: ReportingResource,
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
  const parsed = await readBoundedJsonObject(request, REQUEST_BYTES);
  if (parsed.status === "too_large") return json("invalid_reporting_request", 413);
  if (parsed.status === "invalid") return json("invalid_reporting_request", 400);
  const route = ROUTES[resource];
  const endpoint = new URL(`/api/v1/reporting/${route.path}`, config.apiUrl);
  let upstream: Response;
  try {
    upstream = await fetch(endpoint, {
      body: parsed.bytes,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${config.readToken}`,
        "Content-Type": "application/json",
      },
      method: "POST",
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    return json("reporting_unavailable", 502);
  }
  const contentLength = upstream.headers.get("content-length");
  if (contentLength !== null) {
    if (!/^\d+$/.test(contentLength)) return json("reporting_unavailable", 502);
    const declaredResponse = Number(contentLength);
    if (
      !Number.isSafeInteger(declaredResponse) ||
      declaredResponse > route.responseBytes
    ) {
      return json("reporting_unavailable", 502);
    }
  }
  const response = await readBoundedBytes(upstream.body, route.responseBytes);
  if (response.status !== "ok") return json("reporting_unavailable", 502);
  const responseBody = response.bytes;
  if (!upstream.ok) {
    let error: unknown;
    try {
      error = JSON.parse(Buffer.from(responseBody).toString("utf8"));
    } catch {
      error = null;
    }
    const status =
      upstream.status >= 400 && upstream.status < 500 ? upstream.status : 502;
    return json(safeUpstreamCode(error), status);
  }
  if (!upstream.headers.get("content-type")?.startsWith("application/json")) {
    return json("reporting_unavailable", 502);
  }
  return new Response(responseBody, {
    headers: { "Cache-Control": "no-store", "Content-Type": "application/json" },
    status: 200,
  });
}
