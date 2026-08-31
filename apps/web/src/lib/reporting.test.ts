import { describe, expect, it } from "vitest";

import { initialWindow, queryForRange } from "./reporting";

const NOW = new Date("2026-09-01T12:00:00Z");

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
});
