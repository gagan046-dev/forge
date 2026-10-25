const intFmt = new Intl.NumberFormat("en-US");

export function formatInt(n: number | null | undefined): string {
  return n === null || n === undefined ? "–" : intFmt.format(Math.round(n));
}

/** 1234 -> "1.2k", 2_000_000 -> "2M"; for tight table cells and axes. */
export function formatCompact(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${trim(n / 1e9)}B`;
  if (abs >= 1e6) return `${trim(n / 1e6)}M`;
  if (abs >= 1e3) return `${trim(n / 1e3)}k`;
  return intFmt.format(Math.round(n));
}

function trim(n: number): string {
  return n.toFixed(n >= 100 ? 0 : 1).replace(/\.0$/, "");
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  return formatDuration(ms / 1000);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "–";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "–";
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) return `${gib.toFixed(2)} GiB`;
  return `${Math.round(bytes / 1024 ** 2)} MiB`;
}

export function formatRate(tokensPerSecond: number | null | undefined): string {
  if (tokensPerSecond === null || tokensPerSecond === undefined) return "–";
  return `${tokensPerSecond.toFixed(1)} tok/s`;
}

export function formatPercent(fraction: number): string {
  return `${(fraction * 100).toFixed(fraction < 0.1 ? 1 : 0)}%`;
}

/** "3m ago", "2h ago", "Oct 12" — for list timestamps where exact times are noise. */
export function formatRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "–";
  const diff = Math.round((now - t) / 1000);
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86_400) return `${Math.round(diff / 3600)}h ago`;
  if (diff < 7 * 86_400) return `${Math.round(diff / 86_400)}d ago`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "–";
  return new Date(t).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
