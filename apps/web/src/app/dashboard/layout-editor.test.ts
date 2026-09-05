import { DEFAULT_DASHBOARD_LAYOUT_V1 } from "@cli-consumption/contracts";
import { describe, expect, it } from "vitest";

import {
  LAYOUT_HISTORY_LIMIT,
  addWidget,
  createLayoutHistory,
  firstFitWidget,
  layoutHistoryReducer,
  pointerGridDelta,
  removeWidget,
  updateWidget,
} from "./layout-editor";

describe("layout editor model", () => {
  it("maps pointer pixels through actual gap-aware columns and variable rows", () => {
    expect(
      pointerGridDelta(
        {
          columnGap: 16,
          columns: Array.from({ length: 12 }, () => 80),
          rowGap: 12,
          rows: [100, 180, 120, 200],
          startX: 1,
          startY: 1,
        },
        3 * (80 + 16),
        180 + 12 + 120 + 12,
      ),
    ).toEqual({ x: 3, y: 2 });
  });

  it("places duplicate widgets deterministically without collision", () => {
    const widget = firstFitWidget(DEFAULT_DASHBOARD_LAYOUT_V1, "activity");
    expect(widget).toMatchObject({
      id: "activity-1",
      position: { x: 0, y: 8 },
      size: { height: 1, width: 3 },
      type: "activity",
    });
    expect(addWidget(DEFAULT_DASHBOARD_LAYOUT_V1, "activity")?.widgets.at(-1)).toEqual(
      widget,
    );
  });

  it("rejects collisions, grid bounds, invalid sizes, and an empty layout", () => {
    expect(
      updateWidget(DEFAULT_DASHBOARD_LAYOUT_V1, "activity", {
        position: { x: 0, y: 0 },
      }),
    ).toBeNull();
    expect(
      updateWidget(DEFAULT_DASHBOARD_LAYOUT_V1, "activity", {
        position: { x: -1, y: 1 },
      }),
    ).toBeNull();
    expect(
      updateWidget(DEFAULT_DASHBOARD_LAYOUT_V1, "activity", {
        size: { height: 1, width: 2 },
      }),
    ).toBeNull();
    expect(
      removeWidget(
        {
          ...DEFAULT_DASHBOARD_LAYOUT_V1,
          widgets: [DEFAULT_DASHBOARD_LAYOUT_V1.widgets[0]],
        },
        "headline-metrics",
      ),
    ).toBeNull();
  });

  it("keeps reset undoable and bounds undo/redo history", () => {
    let history = createLayoutHistory(DEFAULT_DASHBOARD_LAYOUT_V1);
    const withoutTools = removeWidget(history.present, "tools");
    expect(withoutTools).not.toBeNull();
    history = layoutHistoryReducer(history, {
      layout: withoutTools ?? DEFAULT_DASHBOARD_LAYOUT_V1,
      type: "commit",
    });
    history = layoutHistoryReducer(history, { type: "reset" });
    expect(history.present).toEqual(DEFAULT_DASHBOARD_LAYOUT_V1);
    history = layoutHistoryReducer(history, { type: "undo" });
    expect(history.present.widgets.some((widget) => widget.id === "tools")).toBe(false);
    history = layoutHistoryReducer(history, { type: "redo" });
    expect(history.present).toEqual(DEFAULT_DASHBOARD_LAYOUT_V1);

    for (let index = 0; index < LAYOUT_HISTORY_LIMIT + 5; index += 1) {
      const changed = removeWidget(
        history.present,
        history.present.widgets.at(-1)?.id ?? "",
      );
      if (changed) {
        history = layoutHistoryReducer(history, { layout: changed, type: "commit" });
      } else {
        history = layoutHistoryReducer(history, { type: "reset" });
      }
    }
    expect(history.past.length).toBeLessThanOrEqual(LAYOUT_HISTORY_LIMIT);
  });
});
