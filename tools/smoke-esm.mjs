import { createDashboardCalculations } from "../packages/analytics/dist/index.js";
import { DASHBOARD_DATASET_VERSION } from "../packages/contracts/dist/index.js";
import { formatDuration } from "../packages/ui/dist/index.js";

if (
  typeof createDashboardCalculations !== "function" ||
  DASHBOARD_DATASET_VERSION !== 1 ||
  formatDuration(1_500) !== "1.5 s"
) {
  throw new Error("invalid_typescript_esm_build");
}
