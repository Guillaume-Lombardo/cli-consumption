import { redirect } from "next/navigation";

import { ConfigurationUnavailable } from "../configuration-unavailable";
import { DashboardClient } from "./dashboard-client";
import { dashboardSessionState } from "../../server/session";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const state = await dashboardSessionState();
  if (state === "anonymous") redirect("/login?reason=session");
  if (state === "unavailable") return <ConfigurationUnavailable />;
  return <DashboardClient />;
}
