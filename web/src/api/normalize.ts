/**
 * Lenient readers for admin responses whose row fields SPEC 2.7 names only
 * loosely ("by_key [...]", "gpu (proxied from gpu-exporter)"). Reading them
 * here keeps every page typed and gives one place to adjust if the gateway's
 * field names change.
 */
import type {
  AdminModel,
  BackendState,
  GpuStats,
  ModelsStatus,
  QuotaRow,
  SystemStatus,
  Usage,
  UsageBreakdownRow,
  UsagePoint,
} from "./types";

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

function num(o: Obj, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  }
  return null;
}

function str(o: Obj, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v.length > 0) return v;
    if (typeof v === "number") return String(v);
  }
  return null;
}

function list(v: unknown): Obj[] {
  return Array.isArray(v) ? v.filter(isObj) : [];
}

function backendState(v: unknown): BackendState {
  return v === "ready" || v === "waking" ? v : "sleeping";
}

function breakdown(rows: unknown, labelKeys: string[]): UsageBreakdownRow[] {
  return list(rows).map((r) => ({
    label: str(r, ...labelKeys) ?? "unknown",
    requests: num(r, "requests", "count") ?? 0,
    prompt_tokens: num(r, "prompt_tokens") ?? 0,
    completion_tokens: num(r, "completion_tokens") ?? 0,
    errors: num(r, "errors", "error_count") ?? 0,
  }));
}

function toEpochSeconds(t: unknown): number | null {
  if (typeof t === "number") return t > 1e12 ? Math.round(t / 1000) : t;
  if (typeof t === "string") {
    const ms = Date.parse(t);
    return Number.isNaN(ms) ? null : Math.round(ms / 1000);
  }
  return null;
}

export function normalizeUsage(raw: unknown): Usage {
  const o = isObj(raw) ? raw : {};
  const totals = isObj(o.totals) ? o.totals : {};
  const latency = isObj(o.latency) ? o.latency : {};

  const timeseries: UsagePoint[] = [];
  for (const p of list(o.timeseries)) {
    const t = toEpochSeconds(p.t ?? p.ts ?? p.bucket);
    if (t === null) continue;
    const tokens =
      num(p, "tokens", "total_tokens") ?? (num(p, "prompt_tokens") ?? 0) + (num(p, "completion_tokens") ?? 0);
    timeseries.push({ t, requests: num(p, "requests", "count") ?? 0, tokens });
  }
  timeseries.sort((a, b) => a.t - b.t);

  const today_by_key: QuotaRow[] = list(o.today_by_key).map((r) => ({
    label: str(r, "name", "key_name", "key", "key_id") ?? "unknown",
    used: num(r, "tokens", "used", "tokens_used", "used_tokens") ?? 0,
    quota: num(r, "quota", "daily_token_quota", "limit") ?? 0,
  }));

  return {
    totals: {
      requests: num(totals, "requests") ?? 0,
      prompt_tokens: num(totals, "prompt_tokens") ?? 0,
      completion_tokens: num(totals, "completion_tokens") ?? 0,
      errors: num(totals, "errors") ?? 0,
    },
    by_key: breakdown(o.by_key, ["name", "key_name", "key", "key_id"]),
    by_model: breakdown(o.by_model, ["model", "alias", "name"]),
    timeseries,
    latency: {
      p50_ms: num(latency, "p50_ms"),
      p95_ms: num(latency, "p95_ms"),
      ttft_p50_ms: num(latency, "ttft_p50_ms"),
      ttft_p95_ms: num(latency, "ttft_p95_ms"),
      tokens_per_s_avg: num(latency, "tokens_per_s_avg"),
    },
    today_by_key,
  };
}

export function normalizeModelsStatus(raw: unknown): ModelsStatus {
  const o = isObj(raw) ? raw : {};
  const rows = Array.isArray(raw) ? list(raw) : list(o.models ?? o.data);
  const models: AdminModel[] = rows.map((m) => {
    const role = str(m, "role");
    return {
      alias: str(m, "alias", "id") ?? "unknown",
      name: str(m, "name") ?? "",
      role: role === "autocomplete" || role === "embedding" ? role : "chat",
      pulled: typeof m.pulled === "boolean" ? m.pulled : null,
      loaded: m.loaded === true,
      size_vram: num(m, "size_vram", "size_vram_bytes"),
    };
  });
  return { backend: backendState(o.backend), models };
}

/** gpu-exporter's /gpu JSON, read by the names of its Prometheus metrics (SPEC 4) or nvidia-smi's columns. */
export function normalizeGpu(raw: unknown): GpuStats | null {
  let o: unknown = raw;
  // gpu-exporter wraps samples as {"up": bool, "gpus": [...]}; a failed nvidia-smi query means no data.
  if (isObj(o) && (o.up === false || o.available === false)) return null;
  if (Array.isArray(o)) o = o[0];
  if (isObj(o) && Array.isArray(o.gpus)) o = o.gpus[0];
  if (!isObj(o)) return null;

  const mib = 1024 * 1024;
  const usedMiB = num(o, "memory_used_mib", "memory_used");
  const totalMiB = num(o, "memory_total_mib", "memory_total");
  return {
    name: str(o, "name"),
    utilization_percent: num(o, "utilization_percent", "utilization_gpu", "utilization"),
    memory_used_bytes: num(o, "memory_used_bytes") ?? (usedMiB !== null ? usedMiB * mib : null),
    memory_total_bytes: num(o, "memory_total_bytes") ?? (totalMiB !== null ? totalMiB * mib : null),
    temperature_celsius: num(o, "temperature_celsius", "temperature_gpu", "temperature"),
    power_watts: num(o, "power_watts", "power_draw", "power_draw_watts"),
    power_limit_watts: num(o, "power_limit_watts", "power_limit"),
  };
}

export function normalizeStatus(raw: unknown): SystemStatus {
  const o = isObj(raw) ? raw : {};
  const q = isObj(o.queue) ? o.queue : isObj(o.queue_depth) ? o.queue_depth : isObj(o.queued) ? o.queued : {};
  const backend = isObj(o.backend) ? o.backend.state : o.backend;
  return {
    backend: backendState(backend ?? o.state),
    queue: {
      autocomplete: num(q, "autocomplete", "AUTOCOMPLETE", "0") ?? 0,
      chat: num(q, "chat", "CHAT", "1") ?? 0,
      batch: num(q, "batch", "BATCH", "2") ?? 0,
    },
    inflight: num(o, "inflight") ?? 0,
    max_inflight: num(o, "max_inflight"),
    demand: num(o, "demand") ?? 0,
    idle_for_s: num(o, "idle_for_s"),
    idle_seconds: num(o, "idle_seconds"),
    gpu: o.gpu == null ? null : normalizeGpu(o.gpu),
  };
}
