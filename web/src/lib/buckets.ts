import type { UsagePoint } from "../api/types";

/**
 * The gateway only returns buckets that had traffic. Filling the gaps with
 * zeros keeps the x axis covering the whole range, so quiet periods are visible.
 * Buckets are aligned in UTC, like the gateway's SQL date_trunc.
 */
export function fillBuckets(points: UsagePoint[], bucket: number, buckets: number, nowSeconds: number): UsagePoint[] {
  const align = (t: number) => Math.floor(t / bucket) * bucket;
  const byBucket = new Map<number, UsagePoint>();
  for (const p of points) {
    const key = align(p.t);
    const prev = byBucket.get(key);
    byBucket.set(key, { t: key, requests: (prev?.requests ?? 0) + p.requests, tokens: (prev?.tokens ?? 0) + p.tokens });
  }
  const last = align(nowSeconds);
  const out: UsagePoint[] = [];
  for (let i = buckets - 1; i >= 0; i--) {
    const t = last - i * bucket;
    out.push(byBucket.get(t) ?? { t, requests: 0, tokens: 0 });
  }
  return out;
}
