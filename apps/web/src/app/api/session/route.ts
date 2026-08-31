import { NextResponse } from "next/server";

import { readBoundedJsonObject } from "../../../server/body";
import {
  dashboardServerConfig,
  type DashboardServerConfig,
} from "../../../server/config";
import {
  createSessionToken,
  passwordMatches,
  sameOrigin,
  sessionCookieName,
  verifySessionToken,
} from "../../../server/session";

const JSON_HEADERS = {
  "Cache-Control": "no-store",
  "Content-Type": "application/json",
};

export async function POST(request: Request) {
  let config: DashboardServerConfig;
  try {
    config = dashboardServerConfig();
  } catch {
    return NextResponse.json(
      { detail: "service_unavailable" },
      { status: 503, headers: JSON_HEADERS },
    );
  }
  if (!sameOrigin(request, config.dashboardOrigin)) {
    return NextResponse.json(
      { detail: "authentication_failed" },
      { status: 403, headers: JSON_HEADERS },
    );
  }
  const body = await readBoundedJsonObject(request, 4096);
  if (body.status !== "ok") {
    return NextResponse.json(
      { detail: "authentication_failed" },
      { status: 400, headers: JSON_HEADERS },
    );
  }
  const password = body.body.password;
  if (
    typeof password !== "string" ||
    password.length > 1024 ||
    !passwordMatches(password, config.password)
  ) {
    return NextResponse.json(
      { detail: "authentication_failed" },
      { status: 401, headers: JSON_HEADERS },
    );
  }
  const response = NextResponse.json(
    { status: "authenticated" },
    { headers: JSON_HEADERS },
  );
  response.cookies.set(sessionCookieName(), createSessionToken(config.sessionSecret), {
    httpOnly: true,
    maxAge: 8 * 60 * 60,
    path: "/",
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
  });
  return response;
}

export async function DELETE(request: Request) {
  let config: DashboardServerConfig;
  try {
    config = dashboardServerConfig();
  } catch {
    return NextResponse.json(
      { detail: "service_unavailable" },
      { status: 503, headers: JSON_HEADERS },
    );
  }
  const cookieHeader = request.headers.get("cookie") ?? "";
  const token = cookieHeader
    .split(";")
    .map((item) => item.trim().split("="))
    .find(([name]) => name === sessionCookieName())?.[1];
  if (
    !sameOrigin(request, config.dashboardOrigin) ||
    !verifySessionToken(token, config.sessionSecret)
  ) {
    return NextResponse.json(
      { detail: "authentication_failed" },
      { status: 401, headers: JSON_HEADERS },
    );
  }
  const response = NextResponse.json(
    { status: "signed_out" },
    { headers: JSON_HEADERS },
  );
  response.cookies.set(sessionCookieName(), "", {
    httpOnly: true,
    maxAge: 0,
    path: "/",
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
  });
  return response;
}
