import { describe, expect, it } from "vitest";
import { LIMIT_BOUNDS, parseLimit } from "./limits";

describe("parseLimit", () => {
  it("treats blank as no value and accepts digit separators", () => {
    expect(parseLimit("  ", LIMIT_BOUNDS.rpm)).toEqual({ ok: true, value: null });
    expect(parseLimit("2,000,000", LIMIT_BOUNDS.dailyTokens)).toEqual({ ok: true, value: 2_000_000 });
    expect(parseLimit("2_000 000", LIMIT_BOUNDS.dailyTokens)).toEqual({ ok: true, value: 2_000_000 });
  });

  it("rejects words and fractions instead of dropping or rounding them", () => {
    expect(parseLimit("abc", LIMIT_BOUNDS.rpm).ok).toBe(false);
    expect(parseLimit("1.6", LIMIT_BOUNDS.rpm).ok).toBe(false);
    expect(parseLimit("-5", LIMIT_BOUNDS.rpm).ok).toBe(false);
    expect(parseLimit("1e6", LIMIT_BOUNDS.rpm).ok).toBe(false);
  });

  it("enforces the gateway's bounds", () => {
    expect(parseLimit("0", LIMIT_BOUNDS.rpm)).toEqual({ ok: false, error: "must be at least 1." });
    expect(parseLimit("100001", LIMIT_BOUNDS.rpm).ok).toBe(false);
    expect(parseLimit("99999999999999999999", LIMIT_BOUNDS.dailyTokens).ok).toBe(false);
    expect(parseLimit("1000000000000000", LIMIT_BOUNDS.dailyTokens)).toEqual({ ok: true, value: 1e15 });
  });
});
