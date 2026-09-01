import { cookies } from "next/headers";

import { proxyOfflineExport } from "../../../../server/export";
import { sessionCookieName } from "../../../../server/session";

export async function POST(request: Request) {
  const store = await cookies();
  return proxyOfflineExport(request, store.get(sessionCookieName())?.value);
}
