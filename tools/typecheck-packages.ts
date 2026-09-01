import {
  DASHBOARD_DATASET_VERSION,
  type DashboardDatasetV1,
} from "@cli-consumption/contracts";
import { createDashboardCalculations } from "@cli-consumption/analytics";
import { formatDuration } from "@cli-consumption/ui";
import { Bars, Metric, Section } from "@cli-consumption/ui/react";

const contractVersion: 1 = DASHBOARD_DATASET_VERSION;
declare const dataset: DashboardDatasetV1;

createDashboardCalculations(dataset);
formatDuration(contractVersion);
void Bars;
void Metric;
void Section;
