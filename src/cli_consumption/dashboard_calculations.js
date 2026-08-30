(function (root) {
  "use strict";

  /**
   * Build the pure calculation contract used by the self-contained dashboard.
   * The input is the already-minimized dashboard payload; no I/O is performed.
   */
  function createDashboardCalculations(data) {
    const conversationByKey = Object.fromEntries(
      data.conversations.map((conversation) => [conversation.key, conversation]),
    );
    const settingByTurn = Object.fromEntries(
      data.turnSettings
        .filter((setting) => setting.turnKey !== null)
        .map((setting) => [setting.turnKey, setting]),
    );

    const validDate = (value) =>
      Boolean(value) && Number.isFinite(Date.parse(value));
    const day = (value) => (validDate(value) ? value.slice(0, 10) : "unknown");
    const total = (rows, key) =>
      rows.reduce((sum, row) => sum + (Number(row[key]) || 0), 0);
    const ratio = (numerator, denominator) =>
      denominator ? (100 * numerator) / denominator : 0;

    /** Return a linearly interpolated percentile, or null without measurements. */
    function percentile(values, probability) {
      const sorted = values
        .map(Number)
        .filter(Number.isFinite)
        .sort((left, right) => left - right);
      if (!sorted.length) return null;
      const index = (sorted.length - 1) * probability;
      const lower = Math.floor(index);
      const upper = Math.ceil(index);
      return (
        sorted[lower] + (sorted[upper] - sorted[lower]) * (index - lower)
      );
    }

    /**
     * Resolve an inclusive UTC display range and an equally sized previous range.
     * Custom dates are YYYY-MM-DD strings; the export window remains authoritative.
     */
    function rangeFor(period, custom = {}) {
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
      const dates = timestampValues
        .filter(validDate)
        .map(Date.parse);
      const addEpoch = (value) => {
        if (value === null || value === undefined) return;
        const epoch = Number(value);
        if (Number.isFinite(epoch)) dates.push(epoch);
      };
      data.workItems.forEach((item) => addEpoch(item.startedAtMs));
      data.subagents.forEach((subagent) => addEpoch(subagent.createdAtMs));
      if (!dates.length) return null;

      const exportStart = validDate(data.meta.exportWindow?.since)
        ? new Date(data.meta.exportWindow.since)
        : null;
      const exclusiveEnd = validDate(data.meta.exportWindow?.until)
        ? new Date(data.meta.exportWindow.until)
        : null;
      const exportEnd = exclusiveEnd ? new Date(exclusiveEnd - 1) : null;
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
        start = custom.from
          ? new Date(`${custom.from}T00:00:00Z`)
          : exportStart;
        end = custom.to ? new Date(`${custom.to}T23:59:59.999Z`) : maximum;
      } else if (period !== "all") {
        start = new Date(maximum);
        start.setUTCDate(start.getUTCDate() - Number(period) + 1);
        start.setUTCHours(0, 0, 0, 0);
      }
      if (exportStart && (!start || start < exportStart)) start = exportStart;
      if (exportEnd && end > exportEnd) end = exportEnd;
      if (!start) return { start: null, end, previous: null };

      const width = end - start;
      const previous = {
        start: new Date(start - width - 1),
        end: new Date(start - 1),
      };
      return {
        start,
        end,
        previous: exportStart && previous.start < exportStart ? null : previous,
      };
    }

    function inRange(value, range) {
      if (!range || !validDate(value)) return !range || !range.start;
      const time = Date.parse(value);
      return (!range.start || time >= range.start) && time <= range.end;
    }

    function inEpochRange(value, range) {
      if (value === null || value === undefined) return !range || !range.start;
      const time = Number(value);
      return (
        (!range || !range.start || time >= range.start.getTime()) &&
        (!range || time <= range.end.getTime())
      );
    }

    function conversationInRange(conversation, range) {
      if (!range) return true;
      if (
        !validDate(conversation.startedAt) &&
        !validDate(conversation.endedAt)
      ) {
        return false;
      }
      const start = validDate(conversation.startedAt)
        ? Date.parse(conversation.startedAt)
        : -Infinity;
      const end = validDate(conversation.endedAt)
        ? Date.parse(conversation.endedAt)
        : Infinity;
      return (
        (!range.start || end >= range.start) &&
        (!range.end || start <= range.end)
      );
    }

    /** Select the complete, related dashboard rows matching filters and range. */
    function selectSlice(filters, range = filters.range) {
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
        if (
          semantics === "conversation-aggregate" ||
          semantics === "context-snapshot"
        ) {
          return conversationInRange(conversation, range);
        }
        return validDate(call.timestamp) && inRange(call.timestamp, range);
      });
      const modelTurns = filters.model
        ? new Set(
            calls.map((call) => call.turnKey).filter((key) => key !== null),
          )
        : null;
      const turns = data.turns.filter(
        (turn) =>
          keys.has(turn.conversationKey) &&
          inRange(turn.startedAt, range) &&
          (!modelTurns || modelTurns.has(turn.key)),
      );
      const allowedTurns = new Set(turns.map((turn) => turn.key));
      calls = calls.filter((call) => {
        const semantics =
          conversationByKey[call.conversationKey]?.tokenSemantics;
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
          (!filters.model ||
            (tool.turnKey !== null && allowedTurns.has(tool.turnKey))),
      );
      const work = data.workItems.filter(
        (item) =>
          keys.has(item.conversationKey) &&
          (!filters.model ||
            (item.turnKey !== null && allowedTurns.has(item.turnKey))) &&
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
            (compaction.turnKey !== null &&
              allowedTurns.has(compaction.turnKey))),
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
      const activeKeys = new Set(
        conversations.map((conversation) => conversation.key),
      );
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

    function activeMs(turns) {
      if (data.meta.shareSafe) return total(turns, "durationMs");
      const groups = {};
      turns.forEach((turn) => {
        const conversation = conversationByKey[turn.conversationKey];
        if (
          !conversation ||
          !validDate(turn.startedAt) ||
          !validDate(turn.endedAt)
        ) {
          return;
        }
        (groups[conversation.machine] ??= []).push([
          Date.parse(turn.startedAt),
          Date.parse(turn.endedAt),
        ]);
      });
      let sum = 0;
      Object.values(groups).forEach((intervals) => {
        intervals.sort((left, right) => left[0] - right[0]);
        let current = null;
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

    function maxConcurrent(turns) {
      if (data.meta.shareSafe) return null;
      const byMachine = {};
      turns.forEach((turn) => {
        const conversation = conversationByKey[turn.conversationKey];
        if (
          !conversation ||
          !validDate(turn.startedAt) ||
          !validDate(turn.endedAt)
        ) {
          return;
        }
        (byMachine[conversation.machine] ??= []).push(
          [Date.parse(turn.startedAt), 1],
          [Date.parse(turn.endedAt), -1],
        );
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

    function semanticTokenCalls(slice) {
      const closedKeys = new Set(
        slice.turns
          .filter(
            (turn) => turn.status === "completed" || turn.status === "aborted",
          )
          .map((turn) => turn.key),
      );
      return slice.calls.filter((call) => {
        const semantics =
          conversationByKey[call.conversationKey]?.tokenSemantics;
        if (semantics === "unavailable") return false;
        if (
          semantics === "conversation-aggregate" ||
          semantics === "context-snapshot"
        ) {
          return true;
        }
        return (
          semantics === "additive" &&
          (call.turnKey === null || closedKeys.has(call.turnKey))
        );
      });
    }

    /** Aggregate the exact metric values displayed by dashboard cards and tables. */
    function metrics(slice) {
      const closed = slice.turns.filter(
        (turn) => turn.status === "completed" || turn.status === "aborted",
      );
      const tokenCalls = semanticTokenCalls(slice);
      const durations = closed
        .map((turn) => turn.durationMs)
        .filter((value) => value !== null);
      const ttfts = closed
        .map((turn) => turn.ttftMs)
        .filter((value) => value !== null);
      const additiveTurns = closed.filter(
        (turn) =>
          conversationByKey[turn.conversationKey]?.tokenSemantics === "additive",
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
            (100 * Number(sample.inputTokens)) /
            Number(sample.contextWindowTokens),
        )
        .filter(Number.isFinite);
      const activeDays = new Set(
        slice.turns
          .map((turn) => day(turn.startedAt))
          .filter((value) => value !== "unknown"),
      ).size;
      return {
        turns: slice.turns.length,
        completed: slice.turns.filter((turn) => turn.status === "completed")
          .length,
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
        reasoningShare: ratio(
          total(tokenCalls, "reasoning_output_tokens"),
          output,
        ),
        activeMs: active,
        throughput: active ? (3600000 * closed.length) / active : 0,
        pressureP50: percentile(pressures, 0.5),
        pressureP95: percentile(pressures, 0.95),
        activeDays,
      };
    }

    /** Compare a metric with a non-zero previous value for display. */
    function compareMetric(current, previous, preference = "neutral") {
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
        preference === "higher"
          ? change > 0
          : preference === "lower"
            ? change < 0
            : null;
      return {
        change,
        style: better === null ? "neutral" : better ? "better" : "worse",
      };
    }

    function cohortLabel(turn, dimension, slice) {
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
    function cohortComparison(slice, dimension) {
      const rows = {};
      const pressureByTurn = {};
      slice.contexts.forEach((sample) => {
        if (sample.turnKey === null) return;
        (pressureByTurn[sample.turnKey] ??= []).push(
          (100 * Number(sample.inputTokens)) /
            Number(sample.contextWindowTokens),
        );
      });
      slice.turns
        .filter(
          (turn) => turn.status === "completed" || turn.status === "aborted",
        )
        .forEach((turn) => {
          const label = cohortLabel(turn, dimension, slice);
          const row = (rows[label] ??= {
            turns: [],
            durations: [],
            tokens: [],
            tools: [],
            pressures: [],
            aborted: 0,
          });
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
          toolsPerTurn: total(
            row.tools.map((value) => ({ value })),
            "value",
          ) / row.turns.length,
          pressureP95: percentile(row.pressures, 0.95),
          abortRate: ratio(row.aborted, row.turns.length),
        }));
    }

    return Object.freeze({
      activeMs,
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

  root.createDashboardCalculations = createDashboardCalculations;
})(globalThis);
