import { describe, expect, it } from "vitest";
import { formatDuration, formatPercent } from "../src/index";

describe("shared presentation formatters", () => {
  it("renders missing and bounded numeric values deterministically", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(1_500)).toBe("1.5 s");
    expect(formatPercent(12.345)).toBe("12.3%");
  });
});
