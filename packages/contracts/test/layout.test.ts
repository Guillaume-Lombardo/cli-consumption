import { describe, expect, it } from "vitest";

import {
  assertDashboardLayoutV1,
  dashboardLayoutComposition,
  DASHBOARD_WIDGET_REGISTRY,
  DEFAULT_DASHBOARD_LAYOUT_V1,
  resolveDashboardLayoutV1,
} from "../src/index";

describe("DashboardLayout v1", () => {
  it("round-trips the deterministic legacy default", () => {
    const document = JSON.parse(JSON.stringify(DEFAULT_DASHBOARD_LAYOUT_V1));
    assertDashboardLayoutV1(document);
    expect(resolveDashboardLayoutV1(document)).toEqual(DEFAULT_DASHBOARD_LAYOUT_V1);
    expect(document.widgets.map((widget: { type: string }) => widget.type)).toEqual(
      Object.keys(DASHBOARD_WIDGET_REGISTRY),
    );
    expect(dashboardLayoutComposition(document).map((widget) => widget.type)).toEqual(
      Object.keys(DASHBOARD_WIDGET_REGISTRY),
    );
  });

  it("derives logical order from coordinates instead of serialized array order", () => {
    const document = structuredClone(DEFAULT_DASHBOARD_LAYOUT_V1);
    document.widgets.reverse();

    expect(dashboardLayoutComposition(document).map((widget) => widget.type)).toEqual(
      Object.keys(DASHBOARD_WIDGET_REGISTRY),
    );
  });

  it.each([
    { ...DEFAULT_DASHBOARD_LAYOUT_V1, secret: "CANARY_DO_NOT_PERSIST" },
    { ...DEFAULT_DASHBOARD_LAYOUT_V1, version: 2 },
    { ...DEFAULT_DASHBOARD_LAYOUT_V1, widgets: [] },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [{ ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1], type: "toString" }],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [
        {
          ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1],
          id: "activity-private-project-label",
        },
      ],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [{ ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1], id: "activity-01" }],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [{ ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1], id: "activity-33" }],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [
        {
          ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1],
          position: { x: 10, y: 0 },
        },
      ],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [
        {
          ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1],
          position: { x: 0, y: Number.MAX_SAFE_INTEGER },
        },
      ],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [
        DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1],
        { ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[2], position: { x: 0, y: 1 } },
      ],
    },
    {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: [
        { ...DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1], config: { prompt: "canary" } },
      ],
    },
  ])("rejects malformed and adversarial documents", (document) => {
    expect(() => assertDashboardLayoutV1(document)).toThrowError(
      "invalid_dashboard_layout",
    );
  });

  it("allows only bounded structural suffixes for multiple widget instances", () => {
    const document = structuredClone(DEFAULT_DASHBOARD_LAYOUT_V1);
    const activity = document.widgets[1];
    if (!activity) throw new Error("missing_default_activity_widget");
    document.widgets = [
      activity,
      {
        ...activity,
        id: "activity-2",
        position: { x: 6, y: 1 },
      },
    ];

    expect(() => assertDashboardLayoutV1(document)).not.toThrow();
  });

  it("drops a retired widget without breaking the remaining composition", () => {
    const stored = structuredClone(DEFAULT_DASHBOARD_LAYOUT_V1) as unknown as {
      widgets: Array<Record<string, unknown>>;
    };
    stored.widgets.splice(1, 0, {
      config: {},
      id: "retired",
      position: { x: 0, y: 1 },
      size: { height: 1, width: 6 },
      type: "retired-widget",
    });
    const resolved = resolveDashboardLayoutV1(stored);
    expect(resolved).toEqual(DEFAULT_DASHBOARD_LAYOUT_V1);
  });
});
