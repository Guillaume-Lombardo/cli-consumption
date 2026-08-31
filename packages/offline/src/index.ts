import { createDashboardCalculations as createCalculations } from "@cli-consumption/analytics";

declare global {
  // The classic self-contained dashboard script consumes this global factory.
  var createDashboardCalculations: typeof createCalculations;
}

globalThis.createDashboardCalculations = createCalculations;
