// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { ActivityCatalog } from "../src/react";

const days = Array.from({ length: 364 }, (_, index) => ({
  date: new Date(Date.UTC(2025, 7, 31 + index)).toISOString().slice(0, 10),
  observed: index >= 360 && index <= 362,
  values: index >= 360 && index <= 362 ? { tokens: index === 361 ? 12 : 0 } : {},
}));
const catalog = {
  days,
  availableMetrics: ["tokens" as const],
  currentStreak: 0,
  longestStreak: 1,
  dailyPeakTokens: 12,
  tokenComposition: [
    ["Input", 10],
    ["Cache", 2],
    ["Output", 4],
    ["Reasoning", 1],
  ] as Array<[string, number]>,
  tokenSeries: [
    {
      date: "2026-08-26",
      total: 12,
      providers: { codex: 12 },
      models: { "model-a": 12 },
    },
  ],
  availableBreakdowns: ["provider" as const, "model" as const],
  rankings: {
    models: [["model-a", 17]] as Array<[string, number]>,
    providers: [],
    projects: [],
    tools: [],
  },
};

describe("activity catalog", () => {
  it("exposes exact values, a table alternative, and one roving tab stop", () => {
    const { container } = render(createElement(ActivityCatalog, { catalog }));
    const activeDate = days[361]?.date;
    expect(screen.getByLabelText(`${activeDate}: 12 tokens`)).toHaveAttribute(
      "data-tooltip",
      `${activeDate}: 12 tokens`,
    );
    expect(screen.getByText("Daily values table")).toBeInTheDocument();
    expect(container.querySelectorAll('.activity-cell[tabindex="0"]')).toHaveLength(1);
    const focused = container.querySelector<HTMLButtonElement>(
      '.activity-cell[tabindex="0"]',
    );
    if (!focused) throw new Error("missing_roving_target");
    fireEvent.keyDown(focused, { key: "ArrowUp" });
    expect(container.querySelectorAll('.activity-cell[tabindex="0"]')).toHaveLength(1);
  });

  it("does not render arbitrary content canaries", () => {
    const { container } = render(createElement(ActivityCatalog, { catalog }));
    expect(container.innerHTML).not.toContain("PRIVATE_PROMPT_CANARY");
  });

  it("keeps aggregate charts visible when daily data is unavailable", () => {
    const aggregateOnly = {
      ...catalog,
      availableMetrics: [],
      tokenSeries: [],
      tokenComposition: [["Input", Number.MAX_SAFE_INTEGER]] as Array<[string, number]>,
      rankings: {
        ...catalog.rankings,
        providers: [
          ["provider-with-an-intentionally-very-long-operational-label", 42],
        ] as Array<[string, number]>,
      },
    };
    const { container } = render(
      createElement(ActivityCatalog, { catalog: aggregateOnly }),
    );
    expect(screen.getByText(/No attributable daily measurements/)).toBeInTheDocument();
    expect(
      screen.getByText("provider-with-an-intentionally-very-long-operational-label"),
    ).toBeInTheDocument();
    expect(container.innerHTML).not.toContain("Infinity");
  });
});
