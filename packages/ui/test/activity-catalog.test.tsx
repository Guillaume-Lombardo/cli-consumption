// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import type { DashboardChartCatalog } from "@cli-consumption/contracts";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ActivityCatalog } from "../src/react";

afterEach(cleanup);

const days = Array.from({ length: 364 }, (_, index) => ({
  date: new Date(Date.UTC(2025, 7, 31 + index)).toISOString().slice(0, 10),
  observed: index >= 360 && index <= 362,
  values: index >= 360 && index <= 362 ? { tokens: index === 361 ? 12 : 0 } : {},
}));
const catalog: DashboardChartCatalog = {
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
      date: "2026-08-25",
      total: 0,
      providers: [{ id: "provider:label:0", kind: "label", label: "codex", value: 0 }],
      models: [{ id: "model:label:0", kind: "label", label: "model-a", value: 0 }],
    },
    {
      date: "2026-08-26",
      total: 12,
      providers: [{ id: "provider:label:0", kind: "label", label: "codex", value: 12 }],
      models: [{ id: "model:label:0", kind: "label", label: "model-a", value: 12 }],
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
    expect(
      container.querySelector('.activity-cell[data-tooltip-row-edge="end"]'),
    ).toBeTruthy();
    expect(container.querySelectorAll('.activity-cell[tabindex="0"]')).toHaveLength(1);
    const seriesDays = container.querySelectorAll<HTMLElement>(".token-series-day");
    expect(seriesDays[0]).toHaveStyle({ height: "0%" });
    expect(seriesDays[0]?.children).toHaveLength(0);
    expect(seriesDays[1]).toHaveStyle({ height: "100%" });
    expect(seriesDays[1]?.children.length).toBeGreaterThan(0);
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

  it("derives valid selections and focus again when filtered props change", async () => {
    const { container, rerender } = render(createElement(ActivityCatalog, { catalog }));
    fireEvent.change(screen.getByLabelText("Token series breakdown"), {
      target: { value: "provider" },
    });
    expect(screen.getByLabelText("Token series breakdown")).toHaveValue("provider");
    const nextDays = days.map((row, index) => ({ ...row, observed: index === 10 }));
    rerender(
      createElement(ActivityCatalog, {
        catalog: { ...catalog, days: nextDays, availableBreakdowns: [] },
      }),
    );
    expect(screen.getByLabelText("Token series breakdown")).toHaveValue("overall");
    await waitFor(() =>
      expect(container.querySelector('.activity-cell[tabindex="0"]')).toHaveAttribute(
        "aria-label",
        expect.stringContaining(nextDays[10]?.date ?? "missing"),
      ),
    );
  });

  it("keeps real Other and Overall labels distinct from internal buckets", () => {
    const providers = [
      {
        id: "provider:label:0" as const,
        kind: "label" as const,
        label: "Other",
        value: 100,
      },
      {
        id: "provider:label:1" as const,
        kind: "label" as const,
        label: "Overall",
        value: 90,
      },
      {
        id: "provider:label:2" as const,
        kind: "label" as const,
        label: "alpha",
        value: 80,
      },
      {
        id: "provider:label:3" as const,
        kind: "label" as const,
        label: "beta",
        value: 70,
      },
      {
        id: "provider:label:4" as const,
        kind: "label" as const,
        label: "gamma",
        value: 60,
      },
      {
        id: "provider:remainder" as const,
        kind: "remainder" as const,
        label: "Other providers",
        value: 90,
      },
    ];
    const { container } = render(
      createElement(ActivityCatalog, {
        catalog: {
          ...catalog,
          tokenSeries: [{ date: "2026-08-26", total: 490, providers, models: [] }],
          availableBreakdowns: ["provider"],
        },
      }),
    );
    fireEvent.change(screen.getByLabelText("Token series breakdown"), {
      target: { value: "provider" },
    });
    fireEvent.click(screen.getByText("Token series table"));
    expect(screen.getByRole("columnheader", { name: "Other tokens" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Overall tokens" })).toBeVisible();
    expect(
      screen.getByRole("columnheader", { name: "Other providers tokens" }),
    ).toBeVisible();
    expect(
      [
        ...container.querySelectorAll(".token-series-panel > .activity-legend span"),
      ].map((node) => node.textContent),
    ).toEqual(["Other", "Overall", "alpha", "beta", "gamma", "Other providers"]);
  });
});
