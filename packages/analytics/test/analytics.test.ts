import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { DashboardDatasetV1 } from "@cli-consumption/contracts";
import { createDashboardCalculations } from "../src/index";

const TOKENS = {
  input_tokens: 100,
  cached_input_tokens: 25,
  cache_write_input_tokens: 0,
  uncached_input_tokens: 75,
  output_tokens: 40,
  reasoning_output_tokens: 10,
  visible_output_tokens: 30,
  unattributed_tokens: 0,
  total_tokens: 140,
};

function fixture(): DashboardDatasetV1 {
  return {
    contractVersion: 1,
    meta: {
      shareSafe: false,
      exportWindow: {
        since: "2026-08-01T00:00:00Z",
        until: "2026-08-03T00:00:00Z",
      },
    },
    conversations: [
      {
        key: 1,
        provider: "codex",
        tokenSemantics: "additive",
        machine: "machine-a",
        project: "project-a",
        models: ["model-a"],
        startedAt: "2026-08-02T01:00:00Z",
        endedAt: "2026-08-02T02:00:00Z",
        durationSeconds: 3_600,
        ...TOKENS,
        turns: 1,
        modelCalls: 1,
        toolCalls: 1,
        compactions: 0,
      },
    ],
    turns: [
      {
        key: 10,
        conversationKey: 1,
        startedAt: "2026-08-02T01:00:00Z",
        endedAt: "2026-08-02T02:00:00Z",
        status: "completed",
        durationMs: 3_600_000,
        ttftMs: 200,
        modelCalls: 1,
        toolCalls: 2,
        ...TOKENS,
      },
    ],
    modelCalls: [
      {
        conversationKey: 1,
        turnKey: 10,
        timestamp: "2026-08-02T01:05:00Z",
        model: "model-a",
        ...TOKENS,
      },
    ],
    toolCalls: [
      {
        conversationKey: 1,
        turnKey: 10,
        sequence: 1,
        timestamp: "2026-08-02T01:10:00Z",
        tool: "Files and workspace",
      },
    ],
    workItems: [],
    contextSamples: [
      {
        conversationKey: 1,
        turnKey: 10,
        timestamp: "2026-08-02T01:05:00Z",
        inputTokens: 100,
        contextWindowTokens: 200,
      },
    ],
    turnSettings: [
      {
        conversationKey: 1,
        turnKey: 10,
        model: "model-a",
        effort: "medium",
        mode: "default",
        tier: null,
        contextWindowTokens: 200,
      },
    ],
    compactions: [],
    subagents: [],
    ingestionRuns: [],
  };
}

describe("shared dashboard analytics", () => {
  it("builds a bounded Sunday-to-Saturday UTC calendar with honest availability", () => {
    const data = fixture();
    const [modelCall] = data.modelCalls;
    if (!modelCall) throw new Error("invalid_test_fixture");
    modelCall.timestamp = "2026-08-01T23:30:00-02:00";
    const calculations = createDashboardCalculations(data);
    const range = calculations.rangeFor("all");
    const catalog = calculations.chartCatalog(
      calculations.selectSlice({
        provider: "",
        machine: "",
        project: "",
        model: "",
        range,
      }),
      range,
    );

    expect(catalog.days).toHaveLength(364);
    expect(new Date(`${catalog.days[0]?.date}T00:00:00Z`).getUTCDay()).toBe(0);
    expect(new Date(`${catalog.days.at(-1)?.date}T00:00:00Z`).getUTCDay()).toBe(6);
    expect(catalog.days.find((row) => row.date === "2026-08-02")?.values.tokens).toBe(
      140,
    );
    expect(catalog.days.at(-1)?.observed).toBe(false);
    expect(catalog.currentStreak).toBe(1);
    expect(catalog.longestStreak).toBe(1);
    expect(catalog.availableMetrics).toEqual([
      "tokens",
      "turns",
      "conversations",
      "duration",
    ]);
    expect(catalog.tokenComposition).toEqual([
      ["Input", 75],
      ["Cache", 25],
      ["Output", 30],
      ["Reasoning", 10],
    ]);
  });

  it("does not fabricate daily tokens from unattributable aggregate counters", () => {
    const data = fixture();
    const [conversation] = data.conversations;
    const [modelCall] = data.modelCalls;
    if (!conversation || !modelCall) throw new Error("invalid_test_fixture");
    conversation.tokenSemantics = "conversation-aggregate";
    modelCall.timestamp = null;
    const calculations = createDashboardCalculations(data);
    const range = calculations.rangeFor("all");
    const catalog = calculations.chartCatalog(
      calculations.selectSlice({
        provider: "",
        machine: "",
        project: "",
        model: "",
        range,
      }),
      range,
    );
    expect(
      calculations.metrics(
        calculations.selectSlice({
          provider: "",
          machine: "",
          project: "",
          model: "",
          range,
        }),
      ).tokens,
    ).toBe(140);
    expect(catalog.availableMetrics).not.toContain("tokens");
    expect(catalog.dailyPeakTokens).toBeNull();
  });

  it("clips chart observations to 52 weeks while preserving global totals", () => {
    const data = fixture();
    data.meta.exportWindow = {
      since: "2025-01-01T00:00:00Z",
      until: "2026-08-03T00:00:00Z",
    };
    const current = data.modelCalls[0];
    if (!current) throw new Error("invalid_test_fixture");
    data.modelCalls.push({
      ...current,
      timestamp: "2025-02-01T00:00:00Z",
      total_tokens: 100_000,
    });
    const calculations = createDashboardCalculations(data);
    const range = calculations.rangeFor("all");
    const slice = calculations.selectSlice({
      provider: "",
      machine: "",
      project: "",
      model: "",
      range,
    });
    const catalog = calculations.chartCatalog(slice, range);
    expect(calculations.metrics(slice).tokens).toBe(100_140);
    expect(catalog.dailyPeakTokens).toBe(140);
    expect(catalog.tokenSeries).toHaveLength(358);
    expect(catalog.tokenSeries.at(-1)?.date).toBe("2026-08-02");
    expect(catalog.days.some((row) => row.date === "2025-02-01")).toBe(false);
  });

  it("represents leap day as one stable UTC cell", () => {
    const data = fixture();
    data.meta.exportWindow = {
      since: "2024-02-28T00:00:00Z",
      until: "2024-03-02T00:00:00Z",
    };
    for (const conversation of data.conversations)
      conversation.startedAt = "2024-02-29T12:00:00Z";
    for (const turn of data.turns) turn.startedAt = "2024-02-29T12:00:00Z";
    for (const call of data.modelCalls) call.timestamp = "2024-02-29T12:00:00Z";
    const calculations = createDashboardCalculations(data);
    const range = calculations.rangeFor("all");
    const catalog = calculations.chartCatalog(
      calculations.selectSlice({
        provider: "",
        machine: "",
        project: "",
        model: "",
        range,
      }),
      range,
    );
    expect(catalog.days.filter((row) => row.date === "2024-02-29")).toHaveLength(1);
  });

  it("keeps percentile and comparison edge cases explicit", () => {
    const calculations = createDashboardCalculations(fixture());

    expect(calculations.percentile([], 0.5)).toBeNull();
    expect(calculations.percentile([20, 10], 0.5)).toBe(15);
    expect(calculations.percentile([10, "invalid", 30], 0.75)).toBe(25);
    expect(calculations.compareMetric(120, 100, "higher")).toEqual({
      change: 20,
      style: "better",
    });
    expect(calculations.compareMetric(80, 100, "lower")).toEqual({
      change: -20,
      style: "better",
    });
    expect(calculations.compareMetric(80, 100)).toEqual({
      change: -20,
      style: "neutral",
    });
    expect(calculations.compareMetric(80, 0, "lower")).toBeNull();
  });

  it("clips periods to the export window without using wall-clock time", () => {
    const data = fixture();
    const calculations = createDashboardCalculations(data);
    const latest = calculations.rangeFor("2");
    const custom = calculations.rangeFor("custom", {
      from: "2026-07-01",
      to: "2026-09-01",
    });

    expect(latest?.start?.toISOString()).toBe("2026-08-01T00:00:00.000Z");
    expect(latest?.end.toISOString()).toBe("2026-08-02T23:59:59.999Z");
    expect(latest?.previous).toBeNull();
    expect(custom?.start?.toISOString()).toBe("2026-08-01T00:00:00.000Z");
    expect(custom?.end.toISOString()).toBe("2026-08-02T23:59:59.999Z");

    const empty = fixture();
    empty.conversations = [];
    empty.turns = [];
    empty.modelCalls = [];
    empty.toolCalls = [];
    empty.workItems = [];
    empty.contextSamples = [];
    empty.turnSettings = [];
    empty.compactions = [];
    empty.subagents = [];
    empty.ingestionRuns = [];
    expect(createDashboardCalculations(empty).rangeFor("30")).toBeNull();
  });

  it("handles the maximum timestamped selection without argument spreading", () => {
    const data = fixture();
    data.workItems = Array.from({ length: 150_000 }, (_, index) => ({
      conversationKey: 1,
      turnKey: 10,
      startedAtMs: Date.parse("2026-08-01T00:00:00Z") + index,
      durationMs: 1,
      kind: "tool",
      tool: null,
      status: "completed",
    }));

    const range = createDashboardCalculations(data).rangeFor("30");
    expect(range?.start?.toISOString()).toBe("2026-08-01T00:00:00.000Z");
    expect(range?.end.toISOString()).toBe("2026-08-02T23:59:59.999Z");
  });

  it("matches the reference metric semantics without mutating the dataset", () => {
    const data = fixture();
    const before = structuredClone(data);
    const calculations = createDashboardCalculations(data);
    const range = calculations.rangeFor("all");
    const slice = calculations.selectSlice({
      provider: "",
      machine: "",
      project: "",
      model: "",
      range,
    });

    expect(calculations.metrics(slice)).toEqual({
      turns: 1,
      completed: 1,
      aborted: 0,
      tokens: 140,
      tokensPerTurn: 140,
      toolsPerTurn: 2,
      cacheRate: 25,
      durationP50: 3_600_000,
      durationP75: 3_600_000,
      durationP95: 3_600_000,
      ttftP50: 200,
      ttftP75: 200,
      ttftP95: 200,
      tokenP75: 140,
      tokenP95: 140,
      toolP75: 2,
      toolP95: 2,
      abortRate: 0,
      reasoningShare: 25,
      activeMs: 3_600_000,
      throughput: 1,
      pressureP50: 50,
      pressureP95: 50,
      activeDays: 1,
    });
    expect(data).toEqual(before);
  });

  it("rejects unknown or incomplete dataset versions", () => {
    expect(() => createDashboardCalculations({ contractVersion: 2 })).toThrow(
      "invalid_dashboard_dataset",
    );
    expect(() => createDashboardCalculations({ contractVersion: 1 })).toThrow(
      "invalid_dashboard_dataset",
    );
  });

  it("contains no DOM, storage, or network primitive", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../src/index.ts", import.meta.url)),
      "utf8",
    );
    for (const forbidden of [
      "fetch(",
      "XMLHttpRequest",
      "WebSocket",
      "document.",
      "navigator.",
      "localStorage",
      "sessionStorage",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });
});
