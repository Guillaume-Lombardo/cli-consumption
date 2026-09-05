import { cookies } from "next/headers";

import { proxyLayoutRequest } from "../../../server/reporting";
import { sessionCookieName } from "../../../server/session";

async function proxy(request: Request) {
  const store = await cookies();
  return proxyLayoutRequest(request, store.get(sessionCookieName())?.value);
}

export const GET = proxy;
export const PUT = proxy;
export const DELETE = proxy;
