/** Parsing the rate-limit and quota inputs on the API keys page. */

export interface LimitBounds {
  min: number;
  max: number;
}

/** Mirrors the gateway's validation (routes/admin.py: MAX_RPM, MAX_DAILY_TOKENS). */
export const LIMIT_BOUNDS = {
  rpm: { min: 1, max: 100_000 },
  dailyTokens: { min: 1, max: 1_000_000_000_000_000 },
} satisfies Record<string, LimitBounds>;

export type ParsedLimit = { ok: true; value: number | null } | { ok: false; error: string };

/**
 * A blank field means "no value" (the default on create); anything else must be
 * a whole number in range. Digit group separators ("2,000,000", "2_000_000",
 * "2 000 000") are accepted; fractions and words are errors, not rounded or dropped.
 */
export function parseLimit(input: string, bounds: LimitBounds): ParsedLimit {
  const text = input.trim().replace(/[_,\s]/g, "");
  if (text === "") return { ok: true, value: null };
  if (!/^\d+$/.test(text)) return { ok: false, error: "must be a whole number." };
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value > bounds.max) {
    return { ok: false, error: `must be at most ${bounds.max.toLocaleString("en-US")}.` };
  }
  if (value < bounds.min) return { ok: false, error: `must be at least ${bounds.min}.` };
  return { ok: true, value };
}
