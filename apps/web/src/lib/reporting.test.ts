import { DEFAULT_DASHBOARD_LAYOUT_V1 } from "@cli-consumption/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchDashboardLayout,
  initialWindow,
  queryForRange,
  saveDashboardLayout,
} from "./reporting";

const NOW = new Date("2026-09-01T12:00:00Z");
const ETAG = '"AAAAAAAAAABSAEZnRrzWfw"';

afterEach(() => vi.restoreAllMocks());

describe("dashboard query construction", () => {
  it("starts with an explicit bounded latest-30-day UTC window", () => {
    expect(initialWindow(NOW)).toEqual({
      since: "2026-08-03T00:00:00.000Z",
      until: "2026-09-02T00:00:00.000Z",
    });
  });

  it("keeps exact operational filters in the POST query", () => {
    const query = queryForRange(
      "7",
      { machines: [], models: [], projects: ["private-project"], providers: ["codex"] },
      { from: "", to: "" },
      NOW,
    );
    expect(query.version).toBe(1);
    expect(query.profile).toBe("detailed");
    expect(query.filters.projects).toEqual(["private-project"]);
    expect(JSON.stringify(query)).toContain("private-project");
  });

  it("preserves the documented exclusive custom end date", () => {
    const query = queryForRange(
      "custom",
      { machines: [], models: [], projects: [], providers: [] },
      { from: "2026-08-01", to: "2026-09-01" },
      NOW,
    );
    expect(query.window).toEqual({
      since: "2026-08-01T00:00:00.000Z",
      until: "2026-09-01T00:00:00.000Z",
    });
  });

  it("resolves a retired widget on GET while preserving its revision ETag", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json(
        {
          columns: 12,
          version: 1,
          widgets: [
            {
              config: {},
              id: "retired-widget",
              position: { x: 0, y: 0 },
              size: { height: 1, width: 6 },
              type: "retired-widget",
            },
          ],
        },
        { headers: { ETag: ETAG } },
      ),
    );

    await expect(fetchDashboardLayout()).resolves.toEqual({
      etag: ETAG,
      layout: DEFAULT_DASHBOARD_LAYOUT_V1,
    });
  });

  it("rejects a retired widget returned by a successful mutation", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json(
        {
          columns: 12,
          version: 1,
          widgets: [
            {
              config: {},
              id: "retired-widget",
              position: { x: 0, y: 0 },
              size: { height: 1, width: 6 },
              type: "retired-widget",
            },
          ],
        },
        { headers: { ETag: ETAG } },
      ),
    );

    await expect(
      saveDashboardLayout(DEFAULT_DASHBOARD_LAYOUT_V1, ETAG),
    ).rejects.toThrow();
  });
});
