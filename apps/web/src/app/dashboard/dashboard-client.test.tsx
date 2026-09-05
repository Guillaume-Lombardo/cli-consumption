// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import {
  type DashboardLayoutV1,
  DEFAULT_DASHBOARD_LAYOUT_V1,
} from "@cli-consumption/contracts";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DashboardDatasetResponse } from "../../lib/reporting";
import { DashboardClient } from "./dashboard-client";

const PROJECT = "private-project-label";
const ETAG = '"AAAAAAAAAABSAEZnRrzWfw"';
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
  it("blocks editing until the initial layout baseline and ETag resolve", async () => {
    let resolveLayout: (response: Response) => void = () => undefined;
    const pendingLayout = new Promise<Response>((resolve) => {
      resolveLayout = resolve;
    });
    const savedLayout: DashboardLayoutV1 = {
      ...DEFAULT_DASHBOARD_LAYOUT_V1,
      widgets: DEFAULT_DASHBOARD_LAYOUT_V1.widgets.slice(0, 2),
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/layout")) return pendingLayout;
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations")) {
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      }
      throw new Error("unexpected_test_request");
    });
    const user = userEvent.setup();
    const { container } = render(<DashboardClient />);
    await screen.findByRole("heading", { name: "Activity" });
    const edit = screen.getByRole("button", { name: "Edit dashboard" });

    expect(edit).toBeDisabled();
    expect(edit).toHaveAttribute("aria-busy", "true");
    await user.click(edit);
    expect(
      screen.queryByRole("region", { name: "Dashboard layout editor" }),
    ).toBeNull();

    resolveLayout(Response.json(savedLayout, { headers: { ETag: ETAG } }));
    await waitFor(() => expect(edit).toBeEnabled());
    await user.click(edit);
    expect(container.querySelectorAll("[data-widget-type]")).toHaveLength(2);
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
  });

  it("announces a non-blocking default-layout fallback without reflecting upstream data", async () => {
    const canary = "PRIVATE_LAYOUT_UPSTREAM_CANARY";
    let layoutReads = 0;
    let layoutWrites = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/layout")) {
        if (init?.method === "PUT") {
          layoutWrites += 1;
          return Response.json(JSON.parse(String(init.body)), {
            headers: { ETag: '"AAAAAAAAAAEArQ90u4jjew"' },
          });
        }
        layoutReads += 1;
        return layoutReads === 1
          ? Response.json({ detail: canary }, { status: 503 })
          : Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
              headers: { ETag: ETAG },
            });
      }
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations")) {
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      }
      throw new Error("unexpected_test_request");
    });

    const user = userEvent.setup();
    const { container } = render(<DashboardClient />);
    const message = await screen.findByText(
      "The saved layout could not be loaded. The default layout is displayed.",
    );

    expect(message.closest("[role='status']")).toHaveTextContent(
      "Saved layout unavailable.",
    );
    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("[data-widget-type]")).toHaveLength(12);
    expect(document.body).not.toHaveTextContent(canary);
    expect(screen.getByRole("button", { name: "Reload saved layout" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Edit dashboard" }));
    await user.click(screen.getByRole("button", { name: "Remove Tools" }));
    await user.click(screen.getByRole("button", { name: "Save layout" }));
    expect(
      await screen.findByText(
        "Layout saving is unavailable. Reload the saved layout and retry.",
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Retry with latest revision" }),
    );
    await waitFor(() => expect(layoutWrites).toBe(1));
    expect(await screen.findByText("Layout saved.")).toBeInTheDocument();
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
  });

  it("uses the resolved layout for visibility, order, and relative geometry", async () => {
    const layout: DashboardLayoutV1 = {
      columns: 12,
      version: 1,
      widgets: [
        {
          config: {},
          id: "activity-2",
          position: { x: 6, y: 1 },
          size: { height: 2, width: 6 },
          type: "activity",
        },
        {
          config: {},
          id: "technical-work-items",
          position: { x: 0, y: 0 },
          size: { height: 1, width: 6 },
          type: "technical-work-items",
        },
      ],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/layout"))
        return Response.json(layout, { headers: { ETag: ETAG } });
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations")) {
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      }
      throw new Error("unexpected_test_request");
    });

    const { container } = render(<DashboardClient />);
    await screen.findByRole("heading", { name: "Technical work items" });
    const widgets = [...container.querySelectorAll<HTMLElement>("[data-widget-type]")];

    expect(widgets.map((widget) => widget.dataset.widgetType)).toEqual([
      "technical-work-items",
      "activity",
    ]);
    expect(
      widgets.map((widget) => ({
        height: widget.dataset.sizeHeight,
        width: widget.dataset.sizeWidth,
        x: widget.dataset.positionX,
        y: widget.dataset.positionY,
      })),
    ).toEqual([
      { height: "1", width: "6", x: "0", y: "0" },
      { height: "2", width: "6", x: "6", y: "1" },
    ]);
    expect(screen.getByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Models" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Conversation explorer" }),
    ).not.toBeInTheDocument();
  });

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
    expect(
      screen.queryByRole("region", { name: "Dashboard layout editor" }),
    ).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Widget catalog" })).toBeNull();
  });

  it("keeps an undoable draft across filters and saves it with its baseline ETag", async () => {
    const requests: Array<{ body: DashboardLayoutV1; etag: string | null }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/layout")) {
        if (init?.method === "PUT") {
          requests.push({
            body: JSON.parse(String(init.body)) as DashboardLayoutV1,
            etag: new Headers(init.headers).get("If-Match"),
          });
          return Response.json(requests.at(-1)?.body, {
            headers: { ETag: '"AAAAAAAAAAEArQ90u4jjew"' },
          });
        }
        return Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
          headers: { ETag: ETAG },
        });
      }
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations"))
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      throw new Error("unexpected_test_request");
    });
    const user = userEvent.setup();
    const { container } = render(<DashboardClient />);
    await screen.findByRole("heading", { name: "Activity" });

    await user.click(screen.getByRole("button", { name: "Edit dashboard" }));
    expect(
      screen.getByRole("complementary", { name: "Widget catalog" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove Tools" }));
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Project" }),
      PROJECT,
    );
    await waitFor(() =>
      expect(container.querySelector('[data-widget-type="tools"]')).toBeNull(),
    );
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(container.querySelector('[data-widget-type="tools"]')).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Redo" }));
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
    for (const storage of [window.localStorage, window.sessionStorage]) {
      const serialized = JSON.stringify({ ...storage });
      expect(serialized).not.toContain("dashboard-layout");
      expect(serialized).not.toContain('"widgets"');
    }
    expect(window.location.href).not.toContain("headline-metrics");
    await user.click(screen.getByRole("button", { name: "Save layout" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]?.etag).toBe(ETAG);
    expect(requests[0]?.body.widgets.some((widget) => widget.type === "tools")).toBe(
      false,
    );
    expect(await screen.findByText("Layout saved.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save layout" })).toBeNull();
  });

  it("preserves a conflicting draft and retries only after loading the latest ETag", async () => {
    let gets = 0;
    let puts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/layout")) {
        if (init?.method === "PUT") {
          puts += 1;
          if (puts === 1)
            return Response.json({ detail: "layout_conflict" }, { status: 412 });
          expect(new Headers(init.headers).get("If-Match")).toBe(
            '"AAAAAAAAAAEArQ90u4jjew"',
          );
          return Response.json(JSON.parse(String(init.body)), {
            headers: { ETag: '"AAAAAAAAAAKceVEjTaNfPw"' },
          });
        }
        gets += 1;
        return Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
          headers: {
            ETag: gets === 1 ? ETAG : '"AAAAAAAAAAEArQ90u4jjew"',
          },
        });
      }
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations"))
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      throw new Error("unexpected_test_request");
    });
    const user = userEvent.setup();
    const { container } = render(<DashboardClient />);
    await screen.findByRole("heading", { name: "Activity" });
    await user.click(screen.getByRole("button", { name: "Edit dashboard" }));
    await user.click(screen.getByRole("button", { name: "Remove Tools" }));
    await user.click(screen.getByRole("button", { name: "Save layout" }));

    expect(
      await screen.findByText(
        "The saved layout changed elsewhere. Your draft is preserved.",
      ),
    ).toBeInTheDocument();
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Retry with latest revision" }),
    );
    await waitFor(() => expect(puts).toBe(2));
    expect(await screen.findByText("Layout saved.")).toBeInTheDocument();
  });

  it("preserves the draft after a fixed network save failure", async () => {
    let puts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/layout")) {
        if (init?.method === "PUT") {
          puts += 1;
          return puts === 1
            ? Response.json(
                { detail: "CANARY_PRIVATE_UPSTREAM_ERROR" },
                { status: 502 },
              )
            : Response.json(JSON.parse(String(init.body)), {
                headers: { ETag: '"AAAAAAAAAAEArQ90u4jjew"' },
              });
        }
        return Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
          headers: { ETag: ETAG },
        });
      }
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations"))
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      throw new Error("unexpected_test_request");
    });
    const user = userEvent.setup();
    const { container } = render(<DashboardClient />);
    await screen.findByRole("heading", { name: "Activity" });
    await user.click(screen.getByRole("button", { name: "Edit dashboard" }));
    await user.click(screen.getByRole("button", { name: "Remove Tools" }));
    await user.click(screen.getByRole("button", { name: "Save layout" }));

    const notice = await screen.findByText(
      "The layout could not be saved. Your draft is preserved.",
    );
    expect(notice).not.toHaveTextContent("CANARY_PRIVATE_UPSTREAM_ERROR");
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
    expect(screen.getByRole("button", { name: "Save layout" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reload saved layout" })).toBeEnabled();
    await user.click(
      screen.getByRole("button", { name: "Retry with latest revision" }),
    );
    await waitFor(() => expect(puts).toBe(2));
    expect(await screen.findByText("Layout saved.")).toBeInTheDocument();
    expect(container.querySelector('[data-widget-type="tools"]')).toBeNull();
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
      if (url.endsWith("/api/layout")) {
        return Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
          headers: { ETag: ETAG },
        });
      }
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
    await user.click(screen.getByRole("button", { name: "Edit dashboard" }));
    await user.click(screen.getByRole("button", { name: "Remove Tools" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Offline export profile" }),
      "share-safe",
    );
    await user.click(screen.getByRole("button", { name: "Export offline" }));
    expect(requests).toHaveLength(0);
    const dialog = screen.getByRole("dialog", { name: "Review offline snapshot" });
    expect(dialog).toHaveTextContent("Share-safe");
    expect(dialog).toHaveTextContent("dark");
    expect(dialog).toHaveTextContent("11 visible widgets");
    expect(dialog).toHaveTextContent("1 active filters");
    expect(dialog).not.toHaveTextContent(PROJECT);
    await user.click(screen.getByRole("button", { name: "Download snapshot" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({
      layout: {
        widgets: expect.not.arrayContaining([
          expect.objectContaining({ type: "tools" }),
        ]),
      },
      query: {
        filters: { machines: [], models: [], projects: [PROJECT], providers: [] },
        profile: "share-safe",
        version: 1,
      },
      theme: "dark",
      version: 1,
    });
    expect(window.location.href).not.toContain(PROJECT);
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:offline-export");
    expect(click).toHaveBeenCalledOnce();
  });

  it("cancels a frozen export preview without making an export request", async () => {
    let exports = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/layout"))
        return Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
          headers: { ETag: ETAG },
        });
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations"))
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      if (url.endsWith("/export")) {
        exports += 1;
        return new Response("offline");
      }
      throw new Error("unexpected_test_request");
    });
    const user = userEvent.setup();
    render(<DashboardClient />);
    const exportButton = await screen.findByRole("button", {
      name: "Export offline",
    });
    await waitFor(() => expect(exportButton).toBeEnabled());
    await user.click(exportButton);
    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(exports).toBe(0);
    expect(exportButton).toHaveFocus();
  });

  it("aborts an in-flight download and closes its private preview", async () => {
    let exports = 0;
    let aborted = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/layout"))
        return Response.json(DEFAULT_DASHBOARD_LAYOUT_V1, {
          headers: { ETag: ETAG },
        });
      if (url.endsWith("/dashboard")) return Response.json(dataset());
      if (url.endsWith("/conversations"))
        return Response.json({ contractVersion: 1, items: [], nextCursor: null });
      if (url.endsWith("/export")) {
        exports += 1;
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            aborted = true;
            reject(new DOMException("aborted", "AbortError"));
          });
        });
      }
      throw new Error("unexpected_test_request");
    });
    const user = userEvent.setup();
    render(<DashboardClient />);
    const exportButton = await screen.findByRole("button", {
      name: "Export offline",
    });
    await waitFor(() => expect(exportButton).toBeEnabled());
    await user.click(exportButton);
    await user.click(screen.getByRole("button", { name: "Download snapshot" }));
    await waitFor(() => expect(exports).toBe(1));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(aborted).toBe(true);
    expect(screen.queryByText("Offline export failed.")).toBeNull();
  });
});
