import { createServer } from "node:http";
import { readFileSync } from "node:fs";

const layout = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/dashboard_layout_v1_custom.json", import.meta.url),
    "utf8",
  ),
);

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
const conversation = {
  key: 1,
  provider: "codex",
  tokenSemantics: "additive",
  machine: "machine-a",
  project: "project-a",
  startedAt: "2026-08-31T01:00:00Z",
  endedAt: "2026-08-31T02:00:00Z",
  durationSeconds: 3600,
  models: ["model-a"],
  turns: 1,
  modelCalls: 1,
  toolCalls: 1,
  compactions: 0,
  ...TOKENS,
};
const turn = {
  key: 10,
  conversationKey: 1,
  startedAt: "2026-08-31T01:00:00Z",
  endedAt: "2026-08-31T02:00:00Z",
  status: "completed",
  durationMs: 3600000,
  ttftMs: 200,
  modelCalls: 1,
  toolCalls: 1,
  ...TOKENS,
};
const call = {
  conversationKey: 1,
  turnKey: 10,
  timestamp: "2026-08-31T01:05:00Z",
  model: "model-a",
  ...TOKENS,
};
const tool = {
  conversationKey: 1,
  turnKey: 10,
  sequence: 1,
  timestamp: "2026-08-31T01:10:00Z",
  tool: "Files and workspace",
};
const dataset = {
  contractVersion: 1,
  meta: {
    shareSafe: false,
    exportWindow: { since: "2026-08-03T00:00:00Z", until: "2026-09-02T00:00:00Z" },
  },
  conversations: [conversation],
  turns: [turn],
  modelCalls: [call],
  toolCalls: [tool],
  workItems: [],
  contextSamples: [
    {
      conversationKey: 1,
      turnKey: 10,
      timestamp: "2026-08-31T01:05:00Z",
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
  window: { since: "2026-08-03T00:00:00Z", until: "2026-09-02T00:00:00Z" },
  profile: "detailed",
  filters: {
    providers: ["codex"],
    machines: ["machine-a"],
    projects: ["project-a"],
    models: ["model-a"],
  },
};

function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
    "Content-Type": "application/json",
  });
  response.end(body);
}

createServer((request, response) => {
  const isExport = request.url === "/api/v1/reporting/export";
  if (request.method === "GET" && request.url === "/api/v1/reporting/layout") {
    if (request.headers.authorization !== "Bearer e2e-read-token") {
      send(response, 401, { detail: "authentication_required" });
      return;
    }
    send(response, 200, layout);
    return;
  }
  if (
    request.method !== "POST" ||
    request.headers.authorization !==
      (isExport ? "Bearer e2e-export-token" : "Bearer e2e-read-token")
  ) {
    send(response, 401, { detail: "authentication_required" });
    return;
  }
  let body = "";
  request.setEncoding("utf8");
  request.on("data", (chunk) => {
    body += chunk;
  });
  request.on("end", () => {
    try {
      const parsed = JSON.parse(body);
      if (parsed.version !== 1 && parsed.query?.version !== 1)
        throw new Error("invalid");
    } catch {
      send(response, 400, { detail: "invalid_reporting_request" });
      return;
    }
    if (isExport) {
      const html = `<!doctype html><meta charset="utf-8"><title>CLI Consumption offline</title><main data-profile="${JSON.parse(body).profile}">project-a offline export</main>`;
      response.writeHead(200, {
        "Cache-Control": "no-store",
        "Content-Length": Buffer.byteLength(html),
        "Content-Type": "text/html; charset=utf-8",
      });
      response.end(html);
    } else if (request.url === "/api/v1/reporting/dashboard") {
      send(response, 200, dataset);
    } else if (request.url === "/api/v1/reporting/conversations") {
      const { key: _key, ...summary } = conversation;
      send(response, 200, {
        contractVersion: 1,
        items: [{ ...summary, conversationRef: "a".repeat(32) }],
        nextCursor: null,
      });
    } else if (request.url === "/api/v1/reporting/conversation") {
      send(response, 200, {
        contractVersion: 1,
        conversation: { ...conversation, key: 0 },
        turns: [{ ...turn, key: 0, conversationKey: 0 }],
        modelCalls: [{ ...call, conversationKey: 0, turnKey: 0 }],
        toolCalls: [{ ...tool, conversationKey: 0, turnKey: 0 }],
        workItems: [],
        contextSamples: [
          {
            conversationKey: 0,
            turnKey: 0,
            timestamp: "2026-08-31T01:05:00Z",
            inputTokens: 100,
            contextWindowTokens: 200,
          },
        ],
        turnSettings: [],
        compactions: [],
      });
    } else {
      send(response, 404, { detail: "reporting_not_found" });
    }
  });
}).listen(4311, "127.0.0.1");
