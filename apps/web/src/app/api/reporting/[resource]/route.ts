import { cookies } from "next/headers";

import {
  isReportingResource,
  proxyReportingRequest,
} from "../../../../server/reporting";
import { sessionCookieName } from "../../../../server/session";

export async function POST(
  request: Request,
  context: { params: Promise<{ resource: string }> },
) {
  const { resource } = await context.params;
  if (!isReportingResource(resource)) {
    return Response.json(
      { detail: "reporting_not_found" },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }
  const store = await cookies();
  return proxyReportingRequest(
    request,
    resource,
    store.get(sessionCookieName())?.value,
  );
}
