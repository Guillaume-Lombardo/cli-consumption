import {
  assertDashboardLayoutV1,
  resolveDashboardLayoutV1,
  type DashboardDatasetV1,
  type DashboardLayoutV1,
} from "@cli-consumption/contracts";

export type RangeChoice = "7" | "30" | "90" | "all" | "custom";

export interface DashboardQueryV1 {
  filters: {
    machines: string[];
    models: string[];
    projects: string[];
    providers: string[];
  };
  profile: "detailed" | "share-safe";
  version: 1;
  window: { since: string | null; until: string | null };
}

export interface DashboardDatasetResponse extends DashboardDatasetV1 {
  filters: {
    machines: string[];
    models: string[];
    projects: string[];
    providers: string[];
  };
  profile: "detailed" | "share-safe";
  window: { since: string | null; until: string | null };
}

export interface ConversationSummary {
  compactions: number;
  conversationRef: string;
  durationSeconds: number | null;
  endedAt: string | null;
  machine: string;
  models: string[];
  project: string;
  provider: string;
  startedAt: string | null;
  tokenSemantics: string;
  total_tokens: number;
  turns: number;
}

export interface ConversationPage {
  contractVersion: 1;
  items: ConversationSummary[];
  nextCursor: string | null;
}

export interface ConversationDetail {
  contractVersion: 1;
  conversation: DashboardDatasetV1["conversations"][number];
  turns: DashboardDatasetV1["turns"];
  modelCalls: DashboardDatasetV1["modelCalls"];
  toolCalls: DashboardDatasetV1["toolCalls"];
  workItems: DashboardDatasetV1["workItems"];
  contextSamples: DashboardDatasetV1["contextSamples"];
  turnSettings: DashboardDatasetV1["turnSettings"];
  compactions: DashboardDatasetV1["compactions"];
}

export function initialWindow(now = new Date()): DashboardQueryV1["window"] {
  const until = new Date(now);
  until.setUTCHours(0, 0, 0, 0);
  until.setUTCDate(until.getUTCDate() + 1);
  const since = new Date(until);
  since.setUTCDate(since.getUTCDate() - 30);
  return { since: since.toISOString(), until: until.toISOString() };
}

export function queryForRange(
  range: RangeChoice,
  filters: DashboardQueryV1["filters"],
  custom: { from: string; to: string },
  now = new Date(),
): DashboardQueryV1 {
  let window = initialWindow(now);
  if (range === "all") window = { since: null, until: null };
  else if (range === "custom") {
    window = {
      since: custom.from ? `${custom.from}T00:00:00.000Z` : null,
      until: custom.to ? `${custom.to}T00:00:00.000Z` : null,
    };
  } else {
    const until = new Date(now);
    until.setUTCHours(0, 0, 0, 0);
    until.setUTCDate(until.getUTCDate() + 1);
    const since = new Date(until);
    since.setUTCDate(since.getUTCDate() - Number(range));
    window = { since: since.toISOString(), until: until.toISOString() };
  }
  return { filters, profile: "detailed", version: 1, window };
}

export async function postReporting<T>(
  resource: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/reporting/${resource}`, {
    body: JSON.stringify(body),
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    method: "POST",
    signal,
  });
  if (response.status === 401) throw new Error("session_expired");
  if (!response.ok) {
    let code = "reporting_unavailable";
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") code = payload.detail;
    } catch {
      // Keep the fixed fallback; upstream text is never surfaced.
    }
    throw new Error(code);
  }
  return (await response.json()) as T;
}

export interface VersionedDashboardLayout {
  etag: string;
  layout: DashboardLayoutV1;
}

async function versionedLayoutResponse(
  response: Response,
): Promise<VersionedDashboardLayout> {
  if (response.status === 401) throw new Error("session_expired");
  if (!response.ok) {
    let code = "reporting_unavailable";
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") code = payload.detail;
    } catch {
      // Keep the fixed fallback; upstream text is never surfaced.
    }
    throw new Error(code);
  }
  const etag = response.headers.get("ETag");
  if (!etag) throw new Error("reporting_unavailable");
  const layout: unknown = await response.json();
  assertDashboardLayoutV1(layout);
  return { etag, layout };
}

export async function fetchDashboardLayout(): Promise<VersionedDashboardLayout> {
  const response = await fetch("/api/layout", { cache: "no-store" });
  const result = await versionedLayoutResponse(response);
  return { ...result, layout: resolveDashboardLayoutV1(result.layout) };
}

export async function saveDashboardLayout(
  layout: DashboardLayoutV1,
  etag: string,
): Promise<VersionedDashboardLayout> {
  assertDashboardLayoutV1(layout);
  const response = await fetch("/api/layout", {
    body: JSON.stringify(layout),
    cache: "no-store",
    headers: { "Content-Type": "application/json", "If-Match": etag },
    method: "PUT",
  });
  return versionedLayoutResponse(response);
}

export async function resetDashboardLayout(
  etag: string,
): Promise<VersionedDashboardLayout> {
  const response = await fetch("/api/layout", {
    cache: "no-store",
    headers: { "If-Match": etag },
    method: "DELETE",
  });
  return versionedLayoutResponse(response);
}

/** Request a self-contained export for the exact query currently shown. */
export async function fetchOfflineExport(query: DashboardQueryV1): Promise<Blob> {
  const response = await fetch("/api/reporting/export", {
    body: JSON.stringify(query),
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
  if (response.status === 401) throw new Error("session_expired");
  if (!response.ok) {
    let code = "reporting_unavailable";
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") code = payload.detail;
    } catch {
      // Keep the fixed fallback; upstream text is never surfaced.
    }
    throw new Error(code);
  }
  return response.blob();
}
