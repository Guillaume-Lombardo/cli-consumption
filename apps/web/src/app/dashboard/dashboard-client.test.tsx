// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DashboardDatasetResponse } from "../../lib/reporting";
import { DashboardClient } from "./dashboard-client";

const PROJECT = "private-project-label";
const TOKENS = {
  cache_write_input_tokens: 0,
  cached_input_tokens: 25,
  input_tokens: 100,
  output_tokens: 40,
  reasoning_output_tokens: 10,
  total_tokens: 140,
  unattributed_tokens: 0,
  uncached_input_tokens: 75,
  visible_output_tokens: 30,
};

function dataset(): DashboardDatasetResponse {
  return {
    compactions: [],
    contextSamples: [
      {
        contextWindowTokens: 200,
        conversationKey: 1,
        inputTokens: 100,
        timestamp: "2026-08-02T01:05:00Z",
        turnKey: 10,
      },
    ],
    contractVersion: 1,
    conversations: [
      {
        ...TOKENS,
        compactions: 0,
        durationSeconds: 3_600,
        endedAt: "2026-08-02T02:00:00Z",
        key: 1,
        machine: "machine-a",
        modelCalls: 1,
        models: ["model-a"],
        project: PROJECT,
        provider: "codex",
        startedAt: "2026-08-02T01:00:00Z",
        tokenSemantics: "additive",
        toolCalls: 1,
        turns: 1,
      },
    ],
    filters: {
      machines: ["machine-a"],
      models: ["model-a"],
      projects: [PROJECT],
      providers: ["codex"],
    },
    ingestionRuns: [],
    meta: {
      exportWindow: {
        since: "2026-08-01T00:00:00Z",
        until: "2026-09-01T00:00:00Z",
      },
      shareSafe: false,
    },
    modelCalls: [
      {
        ...TOKENS,
        conversationKey: 1,
        model: "model-a",
        timestamp: "2026-08-02T01:05:00Z",
        turnKey: 10,
      },
    ],
    profile: "detailed",
    subagents: [],
    toolCalls: [
      {
        conversationKey: 1,
        sequence: 1,
        timestamp: "2026-08-02T01:10:00Z",
        tool: "Files and workspace",
        turnKey: 10,
      },
    ],
    turnSettings: [
      {
        contextWindowTokens: 200,
        conversationKey: 1,
        effort: "medium",
        mode: "default",
        model: "model-a",
        tier: null,
        turnKey: 10,
      },
    ],
    turns: [
      {
        ...TOKENS,
        conversationKey: 1,
        durationMs: 3_600_000,
        endedAt: "2026-08-02T02:00:00Z",
        key: 10,
        modelCalls: 1,
        startedAt: "2026-08-02T01:00:00Z",
        status: "completed",
        toolCalls: 1,
        ttftMs: 200,
      },
    ],
    window: { since: "2026-08-01T00:00:00Z", until: "2026-09-01T00:00:00Z" },
    workItems: [],
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

describe("persistent dashboard", () => {
  it("renders every analytics area with accessible controls", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations")) {
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      }
      throw new Error("unexpected_test_request");
    });
    render(<DashboardClient />);

    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeInTheDocument();
    for (const heading of [
      "Tools",
      "Models",
      "Turn performance",
      "Workflow complexity",
      "Turn outcomes",
      "Context pressure",
      "Technical work items",
      "Cohort comparison",
      "Data quality",
      "Conversation explorer",
    ]) {
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    expect(screen.getByRole("combobox", { name: "Period" })).toHaveValue("30");
    expect(screen.getByRole("button", { name: "Light theme" })).toBeEnabled();
  });

  it("keeps private operational labels out of the shareable URL", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      return Response.json({ contractVersion: 1, items: [], nextCursor: null });
    });
    const user = userEvent.setup();
    render(<DashboardClient />);
    const project = await screen.findByRole("combobox", { name: "Project" });
    await user.selectOptions(project, PROJECT);

    await waitFor(() => expect(project).toHaveValue(PROJECT));
    expect(window.location.search).toBe("?range=30");
    expect(window.location.href).not.toContain(PROJECT);
  });

  it("shows a fixed error without reflecting an upstream canary", async () => {
    const canary = "CANARY_PRIVATE_DRIVER_VALUE";
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ detail: canary }, { status: 500 }),
    );
    render(<DashboardClient />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Reporting is temporarily unavailable.");
    expect(alert).not.toHaveTextContent(canary);
  });

  it("exports the exact visible query with the selected privacy profile", async () => {
    const requests: unknown[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations")) {
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      }
      if (url.endsWith("/export")) {
        requests.push(JSON.parse(String(init?.body)));
        return new Response("<!doctype html><title>Offline</title>", {
          headers: { "Content-Type": "text/html" },
        });
      }
      throw new Error("unexpected_test_request");
    });
    const createObjectURL = vi.fn(() => "blob:offline-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    render(<DashboardClient />);

    const project = await screen.findByRole("combobox", { name: "Project" });
    await user.selectOptions(project, PROJECT);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Offline export profile" }),
      "share-safe",
    );
    await user.click(screen.getByRole("button", { name: "Export offline" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({
      filters: { machines: [], models: [], projects: [PROJECT], providers: [] },
      profile: "share-safe",
      version: 1,
    });
    expect(window.location.href).not.toContain(PROJECT);
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:offline-export");
    expect(click).toHaveBeenCalledOnce();
  });
});
