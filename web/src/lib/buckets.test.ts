import { describe, expect, it } from "vitest";
import { fillBuckets } from "./buckets";

describe("fillBuckets", () => {
  it("covers the whole range with zeros and merges points into their bucket", () => {
    const now = Date.parse("2026-10-25T10:20:00Z") / 1000;
    const out = fillBuckets(
      [
        { t: Date.parse("2026-10-25T09:00:00Z") / 1000, requests: 3, tokens: 30 },
        { t: Date.parse("2026-10-25T09:40:00Z") / 1000, requests: 1, tokens: 5 },
      ],
      3600,
      24,
      now,
    );
    expect(out).toHaveLength(24);
    expect(out[23]).toEqual({ t: Date.parse("2026-10-25T10:00:00Z") / 1000, requests: 0, tokens: 0 });
    expect(out[22]).toEqual({ t: Date.parse("2026-10-25T09:00:00Z") / 1000, requests: 4, tokens: 35 });
    expect(out[0]!.t).toBe(Date.parse("2026-10-24T11:00:00Z") / 1000);
  });
});
