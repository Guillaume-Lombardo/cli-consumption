import "server-only";

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

import { dashboardServerConfig, type DashboardServerConfig } from "./config";

const SESSION_SECONDS = 8 * 60 * 60;
const DEVELOPMENT_COOKIE = "cli-consumption-session";
const PRODUCTION_COOKIE = "__Host-cli-consumption-session";

interface SessionPayload {
  exp: number;
  nonce: string;
  v: 1;
}

export function sessionCookieName(): string {
  return process.env.NODE_ENV === "production" ? PRODUCTION_COOKIE : DEVELOPMENT_COOKIE;
}

function signature(payload: string, secret: string): Buffer {
  return createHmac("sha256", secret).update(payload).digest();
}

export function createSessionToken(secret: string, now = Date.now()): string {
  const payload: SessionPayload = {
    exp: Math.floor(now / 1000) + SESSION_SECONDS,
    nonce: randomBytes(18).toString("base64url"),
    v: 1,
  };
  const encoded = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `${encoded}.${signature(encoded, secret).toString("base64url")}`;
}

export function verifySessionToken(
  token: string | undefined,
  secret: string,
  now = Date.now(),
): boolean {
  if (!token || token.length > 512) return false;
  const [payload, supplied, extra] = token.split(".");
  if (!payload || !supplied || extra !== undefined) return false;
  let suppliedSignature: Buffer;
  try {
    suppliedSignature = Buffer.from(supplied, "base64url");
  } catch {
    return false;
  }
  const expected = signature(payload, secret);
  if (
    suppliedSignature.length !== expected.length ||
    !timingSafeEqual(suppliedSignature, expected)
  ) {
    return false;
  }
  try {
    const decoded = JSON.parse(
      Buffer.from(payload, "base64url").toString("utf8"),
    ) as Partial<SessionPayload>;
    return (
      decoded.v === 1 &&
      typeof decoded.exp === "number" &&
      decoded.exp > Math.floor(now / 1000) &&
      typeof decoded.nonce === "string" &&
      decoded.nonce.length === 24
    );
  } catch {
    return false;
  }
}

export function passwordMatches(supplied: string, expected: string): boolean {
  const suppliedHash = createHmac("sha256", expected).update(supplied).digest();
  const expectedHash = createHmac("sha256", expected).update(expected).digest();
  return timingSafeEqual(suppliedHash, expectedHash);
}

export function sameOrigin(request: Request, configuredOrigin: string | null): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const expected = configuredOrigin ?? new URL(request.url).origin;
  return origin === expected;
}

export type DashboardSessionState = "authenticated" | "anonymous" | "unavailable";

export async function dashboardSessionState(): Promise<DashboardSessionState> {
  let config: DashboardServerConfig;
  try {
    config = dashboardServerConfig();
  } catch {
    return "unavailable";
  }
  const store = await cookies();
  return verifySessionToken(store.get(sessionCookieName())?.value, config.sessionSecret)
    ? "authenticated"
    : "anonymous";
}
