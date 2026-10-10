import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { createDashboardCalculations } from "../src/index";

interface CrosscheckCase {
  name: string;
  dashboardFilter: {
    provider: string;
    machine: string;
    project: string;
    model: string;
  };
  dataset: unknown;
  expected: {
    conversations: number;
    turns: number;
    calls: number;
    inputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
    outputTokens: number;
    reasoningTokens: number;
    totalTokens: number;
    cacheRatePercent: number | null;
  };
}

const fixture = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL("../../../tests/fixtures/usage_report_crosscheck.json", import.meta.url),
    ),
    "utf8",
  ),
) as { cases: CrosscheckCase[] };

describe("terminal usage report cross-check", () => {
  it("covers every token semantic and filter shape", () => {
    expect(fixture.cases.map((testCase) => testCase.name)).toEqual([
      "all-activity",
      "utc-window",
      "intraday-window-provider",
      "until-only-window",
      "model-filter",
      "project-machine-filter",
      "share-safe",
    ]);
  });

  it.each(fixture.cases.map((testCase) => [testCase.name, testCase] as const))(
    "matches dashboard aggregates for %s",
    (_, testCase) => {
      const calculations = createDashboardCalculations(testCase.dataset);
      const range = calculations.rangeFor("all");
      const slice = calculations.selectSlice({ ...testCase.dashboardFilter, range });
      const metrics = calculations.metrics(slice);
      const tokenCalls = calculations.semanticTokenCalls(slice);
      const { expected } = testCase;

      expect(slice.conversations).toHaveLength(expected.conversations);
      expect(metrics.turns).toBe(expected.turns);
      expect(slice.calls).toHaveLength(expected.calls);
      expect(metrics.tokens).toBe(expected.totalTokens);
      expect(calculations.total(tokenCalls, "input_tokens")).toBe(expected.inputTokens);
      expect(calculations.total(tokenCalls, "cached_input_tokens")).toBe(
        expected.cacheReadTokens,
      );
      expect(calculations.total(tokenCalls, "cache_write_input_tokens")).toBe(
        expected.cacheWriteTokens,
      );
      expect(calculations.total(tokenCalls, "output_tokens")).toBe(
        expected.outputTokens,
      );
      expect(calculations.total(tokenCalls, "reasoning_output_tokens")).toBe(
        expected.reasoningTokens,
      );
      // The dashboard shows 0% for an empty denominator; the report shows n/a.
      expect(metrics.cacheRate).toBeCloseTo(expected.cacheRatePercent ?? 0, 4);
    },
  );
});
