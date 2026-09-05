import {
  assertDashboardDatasetV1,
  type DashboardCompaction,
  type DashboardConversation,
  type DashboardContextSample,
  type DashboardDatasetV1,
  type DashboardModelCall,
  type DashboardSubagent,
  type DashboardToolCall,
  type DashboardTurn,
  type DashboardTurnSetting,
  type DashboardWorkItem,
} from "@cli-consumption/contracts";

export interface DashboardRange {
  start: Date | null;
  end: Date;
  previous: { start: Date; end: Date } | null;
}

export interface DashboardFilters {
  provider: string;
  machine: string;
  project: string;
  model: string;
  range: DashboardRange | null;
}

export interface DashboardSlice {
  conversations: DashboardConversation[];
  turns: DashboardTurn[];
  calls: DashboardModelCall[];
  tools: DashboardToolCall[];
  work: DashboardWorkItem[];
  contexts: DashboardContextSample[];
  settings: DashboardTurnSetting[];
  compactions: DashboardCompaction[];
  subagents: DashboardSubagent[];
}

export type ActivityMetric = "tokens" | "turns" | "conversations" | "duration";

export interface ActivityDay {
  date: string;
  values: Partial<Record<ActivityMetric, number>>;
  /** Missing means outside the selected/exported window; zero is an observed empty day. */
  observed: boolean;
}

export interface DashboardChartCatalog {
  days: ActivityDay[];
  availableMetrics: ActivityMetric[];
  currentStreak: number;
  longestStreak: number;
  dailyPeakTokens: number | null;
  tokenComposition: Array<[string, number]>;
  tokenSeries: Array<{
    date: string;
    total: number;
    providers: Record<string, number>;
    models: Record<string, number>;
  }>;
  availableBreakdowns: Array<"provider" | "model">;
  rankings: {
    models: Array<[string, number]>;
    providers: Array<[string, number]>;
    projects: Array<[string, number]>;
    tools: Array<[string, number]>;
  };
}

export const ACTIVITY_CALENDAR_DAYS = 364;

type ComparisonPreference = "higher" | "lower" | "neutral";
type CohortDimension =
  | "project"
  | "model"
  | "effort"
  | "mode"
  | "delegation"
  | "compaction";

/**
 * Build the pure calculation contract used by the self-contained dashboard.
 * The input is the already-minimized dashboard payload; no I/O is performed.
 */
export function createDashboardCalculations(input: unknown) {
  assertDashboardDatasetV1(input);
  const data: DashboardDatasetV1 = input;
  const conversationByKey: Record<number, DashboardConversation> = Object.fromEntries(
    data.conversations.map((conversation) => [conversation.key, conversation]),
  );
  const settingByTurn: Record<number, DashboardTurnSetting> = Object.fromEntries(
    data.turnSettings
      .filter((setting) => setting.turnKey !== null)
      .map((setting) => [setting.turnKey, setting]),
  );

  const validDate = (value: string | null | undefined): value is string =>
    typeof value === "string" && value.length > 0 && Number.isFinite(Date.parse(value));
  const day = (value: string | null | undefined) =>
    validDate(value) ? new Date(value).toISOString().slice(0, 10) : "unknown";
  const total = (rows: readonly object[], key: string) =>
    rows.reduce(
      (sum, row) =>
        sum + (Number((row as unknown as Record<string, unknown>)[key]) || 0),
      0,
    );
  const ratio = (numerator: number, denominator: number) =>
    denominator ? (100 * numerator) / denominator : 0;

  /** Return a linearly interpolated percentile, or null without measurements. */
  function percentile(values: readonly unknown[], probability: number) {
    const sorted = values
      .map(Number)
      .filter(Number.isFinite)
      .sort((left, right) => left - right);
    if (!sorted.length) return null;
    const index = (sorted.length - 1) * probability;
    const lower = Math.floor(index);
    const upper = Math.ceil(index);
    const lowerValue = sorted[lower];
    const upperValue = sorted[upper];
    if (lowerValue === undefined || upperValue === undefined) return null;
    return lowerValue + (upperValue - lowerValue) * (index - lower);
  }

  /**
   * Resolve an inclusive UTC display range and an equally sized previous range.
   * Custom dates are YYYY-MM-DD strings; the export window remains authoritative.
   */
  function rangeFor(
    period: string,
    custom: { from?: string; to?: string } = {},
  ): DashboardRange | null {
    const timestampValues = [
      ...data.conversations.map((conversation) => conversation.startedAt),
      ...data.conversations.map((conversation) => conversation.endedAt),
      ...data.turns.map((turn) => turn.startedAt),
      ...data.turns.map((turn) => turn.endedAt),
      ...data.modelCalls.map((call) => call.timestamp),
      ...data.toolCalls.map((call) => call.timestamp),
      ...data.contextSamples.map((sample) => sample.timestamp),
      ...data.compactions.map((compaction) => compaction.timestamp),
      ...data.ingestionRuns.map((run) => run.ingestedAt),
    ];
    const dates = timestampValues.filter(validDate).map(Date.parse);
    const addEpoch = (value: number | null | undefined) => {
      if (value === null || value === undefined) return;
      const epoch = Number(value);
      if (Number.isFinite(epoch)) dates.push(epoch);
    };
    data.workItems.forEach((item) => {
      addEpoch(item.startedAtMs);
    });
    data.subagents.forEach((subagent) => {
      addEpoch(subagent.createdAtMs);
    });
    if (!dates.length) return null;

    const exportStart = validDate(data.meta.exportWindow?.since)
      ? new Date(data.meta.exportWindow.since)
      : null;
    const exclusiveEnd = validDate(data.meta.exportWindow?.until)
      ? new Date(data.meta.exportWindow.until)
      : null;
    const exportEnd = exclusiveEnd ? new Date(exclusiveEnd.getTime() - 1) : null;
    const latest = dates.reduce(
      (maximum, value) => Math.max(maximum, value),
      -Infinity,
    );
    let maximum = new Date(latest);
    maximum.setUTCHours(23, 59, 59, 999);
    if (exportEnd && exportEnd < maximum) maximum = exportEnd;
    if (exportStart && maximum < exportStart) maximum = exportStart;

    let start = period === "all" ? exportStart : null;
    let end = maximum;
    if (period === "custom") {
      start = custom.from ? new Date(`${custom.from}T00:00:00Z`) : exportStart;
      end = custom.to ? new Date(`${custom.to}T23:59:59.999Z`) : maximum;
    } else if (period !== "all") {
      start = new Date(maximum);
      start.setUTCDate(start.getUTCDate() - Number(period) + 1);
      start.setUTCHours(0, 0, 0, 0);
    }
    if (exportStart && (!start || start < exportStart)) start = exportStart;
    if (exportEnd && end > exportEnd) end = exportEnd;
    if (!start) return { start: null, end, previous: null };

    const width = end.getTime() - start.getTime();
    const previous = {
      start: new Date(start.getTime() - width - 1),
      end: new Date(start.getTime() - 1),
    };
    return {
      start,
      end,
      previous: exportStart && previous.start < exportStart ? null : previous,
    };
  }

  function inRange(value: string | null | undefined, range: DashboardRange | null) {
    if (!range || !validDate(value)) return !range?.start;
    const time = Date.parse(value);
    return (
      (!range.start || time >= range.start.getTime()) && time <= range.end.getTime()
    );
  }

  function inEpochRange(
    value: number | null | undefined,
    range: DashboardRange | null,
  ) {
    if (value === null || value === undefined) return !range?.start;
    const time = Number(value);
    return (
      (!range?.start || time >= range.start.getTime()) &&
      (!range || time <= range.end.getTime())
    );
  }

  function conversationInRange(
    conversation: DashboardConversation,
    range: DashboardRange | null,
  ) {
    if (!range) return true;
    if (!validDate(conversation.startedAt) && !validDate(conversation.endedAt)) {
      return false;
    }
    const start = validDate(conversation.startedAt)
      ? Date.parse(conversation.startedAt)
      : -Infinity;
    const end = validDate(conversation.endedAt)
      ? Date.parse(conversation.endedAt)
      : Infinity;
    return (
      (!range.start || end >= range.start.getTime()) && start <= range.end.getTime()
    );
  }

  /** Select the complete, related dashboard rows matching filters and range. */
  function selectSlice(
    filters: DashboardFilters,
    range: DashboardRange | null = filters.range,
  ): DashboardSlice {
    const base = data.conversations.filter(
      (conversation) =>
        (!filters.provider || conversation.provider === filters.provider) &&
        (!filters.machine || conversation.machine === filters.machine) &&
        (!filters.project || conversation.project === filters.project) &&
        (!filters.model || conversation.models.includes(filters.model)),
    );
    const keys = new Set(base.map((conversation) => conversation.key));
    let calls = data.modelCalls.filter((call) => {
      if (
        !keys.has(call.conversationKey) ||
        (filters.model && call.model !== filters.model)
      ) {
        return false;
      }
      const conversation = conversationByKey[call.conversationKey];
      const semantics = conversation?.tokenSemantics;
      if (semantics === "conversation-aggregate" || semantics === "context-snapshot") {
        return conversation !== undefined && conversationInRange(conversation, range);
      }
      return validDate(call.timestamp) && inRange(call.timestamp, range);
    });
    const modelTurns = filters.model
      ? new Set(calls.map((call) => call.turnKey).filter((key) => key !== null))
      : null;
    const turns = data.turns.filter(
      (turn) =>
        keys.has(turn.conversationKey) &&
        inRange(turn.startedAt, range) &&
        (!modelTurns || modelTurns.has(turn.key)),
    );
    const allowedTurns = new Set(turns.map((turn) => turn.key));
    calls = calls.filter((call) => {
      const semantics = conversationByKey[call.conversationKey]?.tokenSemantics;
      return (
        semantics === "conversation-aggregate" ||
        semantics === "context-snapshot" ||
        call.turnKey === null ||
        allowedTurns.has(call.turnKey)
      );
    });
    const tools = data.toolCalls.filter(
      (tool) =>
        keys.has(tool.conversationKey) &&
        inRange(tool.timestamp, range) &&
        (!filters.model || (tool.turnKey !== null && allowedTurns.has(tool.turnKey))),
    );
    const work = data.workItems.filter(
      (item) =>
        keys.has(item.conversationKey) &&
        (!filters.model || (item.turnKey !== null && allowedTurns.has(item.turnKey))) &&
        inEpochRange(item.startedAtMs, range),
    );
    const contexts = data.contextSamples.filter(
      (sample) =>
        keys.has(sample.conversationKey) &&
        inRange(sample.timestamp, range) &&
        (!filters.model ||
          (sample.turnKey !== null && allowedTurns.has(sample.turnKey))),
    );
    const settings = data.turnSettings.filter(
      (setting) =>
        keys.has(setting.conversationKey) &&
        setting.turnKey !== null &&
        allowedTurns.has(setting.turnKey),
    );
    const compactions = data.compactions.filter(
      (compaction) =>
        keys.has(compaction.conversationKey) &&
        inRange(compaction.timestamp, range) &&
        (!filters.model ||
          (compaction.turnKey !== null && allowedTurns.has(compaction.turnKey))),
    );
    const activeConversationKeys = new Set([
      ...turns.map((turn) => turn.conversationKey),
      ...calls.map((call) => call.conversationKey),
      ...tools.map((tool) => tool.conversationKey),
    ]);
    const conversations = base.filter(
      (conversation) =>
        (conversationInRange(conversation, range) ||
          activeConversationKeys.has(conversation.key)) &&
        (!filters.model ||
          calls.some((call) => call.conversationKey === conversation.key)),
    );
    const activeKeys = new Set(conversations.map((conversation) => conversation.key));
    const subagents = data.subagents.filter(
      (subagent) =>
        subagent.conversationKey !== null &&
        activeKeys.has(subagent.conversationKey) &&
        inEpochRange(subagent.createdAtMs, range),
    );
    return {
      conversations,
      turns,
      calls,
      tools,
      work,
      contexts,
      settings,
      compactions,
      subagents,
    };
  }

  function activeMs(turns: readonly DashboardTurn[]) {
    if (data.meta.shareSafe) return total(turns, "durationMs");
    const groups: Record<string, Array<[number, number]>> = {};
    turns.forEach((turn) => {
      const conversation = conversationByKey[turn.conversationKey];
      if (!conversation || !validDate(turn.startedAt) || !validDate(turn.endedAt)) {
        return;
      }
      const intervals = groups[conversation.machine] ?? [];
      intervals.push([Date.parse(turn.startedAt), Date.parse(turn.endedAt)]);
      groups[conversation.machine] = intervals;
    });
    let sum = 0;
    Object.values(groups).forEach((intervals) => {
      intervals.sort((left, right) => left[0] - right[0]);
      let current: [number, number] | null = null;
      intervals.forEach(([start, end]) => {
        if (!current) current = [start, end];
        else if (start <= current[1]) current[1] = Math.max(current[1], end);
        else {
          sum += current[1] - current[0];
          current = [start, end];
        }
      });
      if (current) sum += current[1] - current[0];
    });
    return sum;
  }

  function maxConcurrent(turns: readonly DashboardTurn[]) {
    if (data.meta.shareSafe) return null;
    const byMachine: Record<string, Array<[number, number]>> = {};
    turns.forEach((turn) => {
      const conversation = conversationByKey[turn.conversationKey];
      if (!conversation || !validDate(turn.startedAt) || !validDate(turn.endedAt)) {
        return;
      }
      const points = byMachine[conversation.machine] ?? [];
      points.push([Date.parse(turn.startedAt), 1], [Date.parse(turn.endedAt), -1]);
      byMachine[conversation.machine] = points;
    });
    let peak = 0;
    Object.values(byMachine).forEach((points) => {
      points.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
      let active = 0;
      points.forEach(([, change]) => {
        active += change;
        peak = Math.max(peak, active);
      });
    });
    return peak;
  }

  function semanticTokenCalls(slice: DashboardSlice) {
    const closedKeys = new Set(
      slice.turns
        .filter((turn) => turn.status === "completed" || turn.status === "aborted")
        .map((turn) => turn.key),
    );
    return slice.calls.filter((call) => {
      const semantics = conversationByKey[call.conversationKey]?.tokenSemantics;
      if (semantics === "unavailable") return false;
      if (semantics === "conversation-aggregate" || semantics === "context-snapshot") {
        return true;
      }
      return (
        semantics === "additive" &&
        (call.turnKey === null || closedKeys.has(call.turnKey))
      );
    });
  }

  /**
   * Build the bounded, provider-neutral chart data shared by online and offline UIs.
   * Calendar boundaries and bucketing are UTC. Aggregate/context-snapshot token
   * counters deliberately do not enter a time series because they have no honest
   * per-day attribution.
   */
  function chartCatalog(
    slice: DashboardSlice,
    range: DashboardRange | null,
  ): DashboardChartCatalog {
    const selectedEnd =
      range?.end ??
      (() => {
        const timestamps = slice.turns.map((turn) => turn.startedAt).filter(validDate);
        return timestamps.length
          ? new Date(Math.max(...timestamps.map(Date.parse)))
          : new Date(0);
      })();
    const endDay = new Date(selectedEnd);
    endDay.setUTCHours(0, 0, 0, 0);
    // Stable Sunday-to-Saturday columns, independent of locale and wall-clock time.
    endDay.setUTCDate(endDay.getUTCDate() + (6 - endDay.getUTCDay()));
    const startDay = new Date(endDay);
    startDay.setUTCDate(startDay.getUTCDate() - ACTIVITY_CALENDAR_DAYS + 1);
    const observedStart = range?.start
      ? new Date(
          Date.UTC(
            range.start.getUTCFullYear(),
            range.start.getUTCMonth(),
            range.start.getUTCDate(),
          ),
        )
      : startDay;
    const observedEnd = range
      ? new Date(
          Date.UTC(
            range.end.getUTCFullYear(),
            range.end.getUTCMonth(),
            range.end.getUTCDate(),
          ),
        )
      : endDay;
    const byDate = new Map<string, ActivityDay>();
    for (let offset = 0; offset < ACTIVITY_CALENDAR_DAYS; offset += 1) {
      const current = new Date(startDay);
      current.setUTCDate(current.getUTCDate() + offset);
      const date = current.toISOString().slice(0, 10);
      const observed = current >= observedStart && current <= observedEnd;
      const row: ActivityDay = { date, observed, values: {} };
      if (observed) row.values = { conversations: 0, duration: 0, tokens: 0, turns: 0 };
      byDate.set(date, row);
    }
    const add = (timestamp: string | null, metric: ActivityMetric, value: number) => {
      if (!validDate(timestamp)) return;
      const row = byDate.get(new Date(timestamp).toISOString().slice(0, 10));
      if (!row?.observed || !Number.isFinite(value)) return;
      row.values[metric] = (row.values[metric] ?? 0) + value;
    };
    const visibleConversations = slice.conversations.filter(
      (conversation) =>
        validDate(conversation.startedAt) &&
        byDate.get(new Date(conversation.startedAt).toISOString().slice(0, 10))
          ?.observed,
    );
    visibleConversations.forEach((conversation) => {
      add(conversation.startedAt, "conversations", 1);
    });
    const visibleTurns = slice.turns.filter(
      (turn) =>
        validDate(turn.startedAt) &&
        byDate.get(new Date(turn.startedAt).toISOString().slice(0, 10))?.observed,
    );
    visibleTurns.forEach((turn) => {
      add(turn.startedAt, "turns", 1);
      if (turn.durationMs !== null) add(turn.startedAt, "duration", turn.durationMs);
    });
    const timedTokenCalls = semanticTokenCalls(slice).filter((call) => {
      const semantics = conversationByKey[call.conversationKey]?.tokenSemantics;
      return (
        semantics === "additive" &&
        validDate(call.timestamp) &&
        byDate.get(new Date(call.timestamp).toISOString().slice(0, 10))?.observed
      );
    });
    timedTokenCalls.forEach((call) => {
      add(call.timestamp, "tokens", call.total_tokens);
    });
    const tokenSeries = [...byDate.values()]
      .filter((row) => row.observed)
      .map((row) => ({
        date: row.date,
        total: row.values.tokens ?? 0,
        providers: {} as Record<string, number>,
        models: {} as Record<string, number>,
      }));
    const seriesByDate = new Map(tokenSeries.map((row) => [row.date, row]));
    for (const call of timedTokenCalls) {
      if (!validDate(call.timestamp)) continue;
      const point = seriesByDate.get(
        new Date(call.timestamp).toISOString().slice(0, 10),
      );
      if (!point) continue;
      const provider = conversationByKey[call.conversationKey]?.provider || "unknown";
      const model = call.model || "unknown";
      point.providers[provider] = (point.providers[provider] ?? 0) + call.total_tokens;
      point.models[model] = (point.models[model] ?? 0) + call.total_tokens;
    }
    const availableBreakdowns: Array<"provider" | "model"> = [];
    if (
      timedTokenCalls.length &&
      timedTokenCalls.every(
        (call) =>
          (conversationByKey[call.conversationKey]?.provider || "unknown") !==
          "unknown",
      )
    )
      availableBreakdowns.push("provider");
    if (
      timedTokenCalls.length &&
      timedTokenCalls.every((call) => Boolean(call.model) && call.model !== "unknown")
    )
      availableBreakdowns.push("model");
    const availableMetrics: ActivityMetric[] = [];
    if (timedTokenCalls.length) availableMetrics.push("tokens");
    if (visibleTurns.length) availableMetrics.push("turns");
    if (visibleConversations.length) availableMetrics.push("conversations");
    if (visibleTurns.some((turn) => turn.durationMs !== null))
      availableMetrics.push("duration");
    const activity = [...byDate.values()].map(
      (row) =>
        row.observed &&
        ((row.values.turns ?? 0) > 0 || (row.values.conversations ?? 0) > 0),
    );
    let longestStreak = 0;
    let running = 0;
    for (const active of activity) {
      running = active ? running + 1 : 0;
      longestStreak = Math.max(longestStreak, running);
    }
    let currentStreak = 0;
    let finalObserved = [...byDate.values()].findLastIndex((row) => row.observed);
    for (; finalObserved >= 0 && activity[finalObserved]; finalObserved -= 1)
      currentStreak += 1;
    const tokenCalls = semanticTokenCalls(slice);
    const rank = (rows: Array<[string, number]>) => {
      const grouped = new Map<string, number>();
      for (const [rawLabel, value] of rows) {
        const label = rawLabel || "unknown";
        grouped.set(label, (grouped.get(label) ?? 0) + value);
      }
      return [...grouped]
        .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
        .slice(0, 10);
    };
    return {
      days: [...byDate.values()],
      availableMetrics,
      currentStreak,
      longestStreak,
      dailyPeakTokens: timedTokenCalls.length
        ? Math.max(...[...byDate.values()].map((row) => row.values.tokens ?? 0))
        : null,
      tokenComposition: [
        [
          "Input",
          total(tokenCalls, "uncached_input_tokens") +
            total(tokenCalls, "cache_write_input_tokens"),
        ],
        ["Cache", total(tokenCalls, "cached_input_tokens")],
        ["Output", total(tokenCalls, "visible_output_tokens")],
        ["Reasoning", total(tokenCalls, "reasoning_output_tokens")],
      ],
      tokenSeries,
      availableBreakdowns,
      rankings: {
        models: rank(
          tokenCalls.map((call) => [call.model || "unknown", call.total_tokens]),
        ),
        providers: rank(
          tokenCalls.map((call) => [
            conversationByKey[call.conversationKey]?.provider || "unknown",
            call.total_tokens,
          ]),
        ),
        projects: rank(
          tokenCalls.map((call) => [
            conversationByKey[call.conversationKey]?.project || "unknown",
            call.total_tokens,
          ]),
        ),
        tools: rank(slice.tools.map((call) => [call.tool || "unknown", 1])),
      },
    };
  }

  /** Aggregate the exact metric values displayed by dashboard cards and tables. */
  function metrics(slice: DashboardSlice) {
    const closed = slice.turns.filter(
      (turn) => turn.status === "completed" || turn.status === "aborted",
    );
    const tokenCalls = semanticTokenCalls(slice);
    const durations = closed
      .map((turn) => turn.durationMs)
      .filter((value) => value !== null);
    const ttfts = closed.map((turn) => turn.ttftMs).filter((value) => value !== null);
    const additiveTurns = closed.filter(
      (turn) => conversationByKey[turn.conversationKey]?.tokenSemantics === "additive",
    );
    const turnTokens = additiveTurns.map((turn) => turn.total_tokens);
    const turnTools = closed.map((turn) => turn.toolCalls);
    const input = total(tokenCalls, "input_tokens");
    const cached = total(tokenCalls, "cached_input_tokens");
    const output = total(tokenCalls, "output_tokens");
    const active = activeMs(closed);
    const pressures = slice.contexts
      .map(
        (sample) =>
          (100 * Number(sample.inputTokens)) / Number(sample.contextWindowTokens),
      )
      .filter(Number.isFinite);
    const activeDays = new Set(
      slice.turns
        .map((turn) => day(turn.startedAt))
        .filter((value) => value !== "unknown"),
    ).size;
    return {
      turns: slice.turns.length,
      completed: slice.turns.filter((turn) => turn.status === "completed").length,
      aborted: slice.turns.filter((turn) => turn.status === "aborted").length,
      tokens: total(tokenCalls, "total_tokens"),
      tokensPerTurn: percentile(turnTokens, 0.5),
      toolsPerTurn: percentile(turnTools, 0.5),
      cacheRate: ratio(cached, input),
      durationP50: percentile(durations, 0.5),
      durationP75: percentile(durations, 0.75),
      durationP95: percentile(durations, 0.95),
      ttftP50: percentile(ttfts, 0.5),
      ttftP75: percentile(ttfts, 0.75),
      ttftP95: percentile(ttfts, 0.95),
      tokenP75: percentile(turnTokens, 0.75),
      tokenP95: percentile(turnTokens, 0.95),
      toolP75: percentile(turnTools, 0.75),
      toolP95: percentile(turnTools, 0.95),
      abortRate: ratio(
        slice.turns.filter((turn) => turn.status === "aborted").length,
        closed.length,
      ),
      reasoningShare: ratio(total(tokenCalls, "reasoning_output_tokens"), output),
      activeMs: active,
      throughput: active ? (3600000 * closed.length) / active : 0,
      pressureP50: percentile(pressures, 0.5),
      pressureP95: percentile(pressures, 0.95),
      activeDays,
    };
  }

  /** Compare a metric with a non-zero previous value for display. */
  function compareMetric(
    current: number,
    previous: number | null | undefined,
    preference: ComparisonPreference = "neutral",
  ) {
    if (
      previous === null ||
      previous === undefined ||
      !Number.isFinite(previous) ||
      previous === 0
    ) {
      return null;
    }
    const change = (100 * (current - previous)) / Math.abs(previous);
    const better =
      preference === "higher" ? change > 0 : preference === "lower" ? change < 0 : null;
    return {
      change,
      style: better === null ? "neutral" : better ? "better" : "worse",
    };
  }

  function cohortLabel(
    turn: DashboardTurn,
    dimension: CohortDimension,
    slice: DashboardSlice,
  ) {
    const conversation = conversationByKey[turn.conversationKey];
    const setting = settingByTurn[turn.key];
    if (dimension === "project") return conversation?.project || "unknown";
    if (dimension === "model") {
      return setting?.model || conversation?.models?.join(", ") || "unknown";
    }
    if (dimension === "effort") return setting?.effort || "unknown";
    if (dimension === "mode") return setting?.mode || "unknown";
    if (dimension === "delegation") {
      return slice.subagents.some(
        (subagent) => subagent.conversationKey === turn.conversationKey,
      )
        ? "delegated"
        : "not delegated";
    }
    if (dimension === "compaction") {
      return slice.compactions.some(
        (compaction) => compaction.conversationKey === turn.conversationKey,
      )
        ? "compacted"
        : "not compacted";
    }
    return "unknown";
  }

  /** Return the exact cohort comparison values rendered by the dashboard. */
  function cohortComparison(slice: DashboardSlice, dimension: CohortDimension) {
    const rows: Record<
      string,
      {
        turns: DashboardTurn[];
        durations: number[];
        tokens: number[];
        tools: number[];
        pressures: number[];
        aborted: number;
      }
    > = {};
    const pressureByTurn: Record<number, number[]> = {};
    slice.contexts.forEach((sample) => {
      if (sample.turnKey === null) return;
      const pressures = pressureByTurn[sample.turnKey] ?? [];
      pressures.push(
        (100 * Number(sample.inputTokens)) / Number(sample.contextWindowTokens),
      );
      pressureByTurn[sample.turnKey] = pressures;
    });
    slice.turns
      .filter((turn) => turn.status === "completed" || turn.status === "aborted")
      .forEach((turn) => {
        const label = cohortLabel(turn, dimension, slice);
        let row = rows[label];
        if (row === undefined) {
          row = {
            turns: [],
            durations: [],
            tokens: [],
            tools: [],
            pressures: [],
            aborted: 0,
          };
          rows[label] = row;
        }
        row.turns.push(turn);
        if (turn.durationMs !== null) row.durations.push(turn.durationMs);
        row.tokens.push(turn.total_tokens);
        row.tools.push(turn.toolCalls);
        for (const pressure of pressureByTurn[turn.key] || []) {
          row.pressures.push(pressure);
        }
        if (turn.status === "aborted") row.aborted += 1;
      });
    return Object.entries(rows)
      .filter(([, row]) => !data.meta.shareSafe || row.turns.length >= 5)
      .sort((left, right) => right[1].turns.length - left[1].turns.length)
      .map(([label, row]) => ({
        label,
        turns: row.turns.length,
        durationP50: percentile(row.durations, 0.5),
        tokensP50: percentile(row.tokens, 0.5),
        toolsPerTurn:
          total(
            row.tools.map((value) => ({ value })),
            "value",
          ) / row.turns.length,
        pressureP95: percentile(row.pressures, 0.95),
        abortRate: ratio(row.aborted, row.turns.length),
      }));
  }

  return Object.freeze({
    activeMs,
    chartCatalog,
    cohortComparison,
    compareMetric,
    conversationByKey,
    day,
    inRange,
    maxConcurrent,
    metrics,
    percentile,
    rangeFor,
    ratio,
    selectSlice,
    semanticTokenCalls,
    total,
    validDate,
  });
}
