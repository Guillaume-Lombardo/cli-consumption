import { DashboardClient } from "./dashboard-client";
import { requireDashboardSession } from "../../server/session";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  await requireDashboardSession();
  return <DashboardClient />;
}
