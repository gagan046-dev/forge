import { describe, expect, it } from "vitest";
import { normalizeGpu, normalizeStatus, normalizeUsage } from "./normalize";

describe("normalizeUsage", () => {
  it("reads the SPEC 2.7 shape and sorts the time series", () => {
    const usage = normalizeUsage({
      totals: { requests: 10, prompt_tokens: 100, completion_tokens: 50, errors: 1 },
      by_key: [{ name: "vigil", requests: 7, prompt_tokens: 70, completion_tokens: 30, errors: 0 }],
      by_model: [{ model: "forge-chat", requests: 10, prompt_tokens: 100, completion_tokens: 50, errors: 1 }],
      timeseries: [
        { t: "2026-10-25T11:00:00Z", requests: 2, tokens: 20 },
        { t: "2026-10-25T10:00:00Z", requests: 1, tokens: 5 },
      ],
      latency: { p50_ms: 800, p95_ms: 2400, ttft_p50_ms: 300, ttft_p95_ms: 900, tokens_per_s_avg: 41.5 },
      today_by_key: [{ name: "vigil", tokens: 1000, quota: 2000000 }],
    });
    expect(usage.totals.requests).toBe(10);
    expect(usage.by_key[0]!.label).toBe("vigil");
    expect(usage.by_model[0]!.label).toBe("forge-chat");
    expect(usage.timeseries.map((p) => p.requests)).toEqual([1, 2]);
    expect(usage.timeseries[0]!.t).toBe(Date.parse("2026-10-25T10:00:00Z") / 1000);
    expect(usage.latency.tokens_per_s_avg).toBe(41.5);
    expect(usage.today_by_key[0]).toEqual({ label: "vigil", used: 1000, quota: 2000000 });
  });

  it("survives an empty body", () => {
    const usage = normalizeUsage(null);
    expect(usage.totals.requests).toBe(0);
    expect(usage.timeseries).toEqual([]);
    expect(usage.latency.p50_ms).toBeNull();
  });
});

describe("normalizeStatus", () => {
  it("reads per-priority queue depth and the GPU block", () => {
    const status = normalizeStatus({
      backend: "ready",
      queue: { autocomplete: 0, chat: 2, batch: 5 },
      inflight: 2,
      demand: 9,
      idle_for_s: 0,
      gpu: { name: "RTX 4070", utilization_gpu: 87, memory_used: 5120, memory_total: 8188, temperature_gpu: 64, power_draw: 71.2, power_limit: 115 },
    });
    expect(status.backend).toBe("ready");
    expect(status.queue).toEqual({ autocomplete: 0, chat: 2, batch: 5 });
    expect(status.gpu?.utilization_percent).toBe(87);
    expect(status.gpu?.memory_used_bytes).toBe(5120 * 1024 * 1024);
  });

  it("treats an unreachable exporter as no GPU data", () => {
    expect(normalizeStatus({ backend: "sleeping", gpu: null }).gpu).toBeNull();
    expect(normalizeGpu({ up: false, gpus: [{ name: "stale" }] })).toBeNull();
  });

  it("reads the gpu-exporter /gpu envelope", () => {
    const gpu = normalizeGpu({
      up: true,
      updated_at: "2026-10-25T10:00:00+00:00",
      gpus: [{ index: 0, name: "RTX 4070", utilization_percent: 40, memory_used_bytes: 1024, memory_total_bytes: 2048, temperature_celsius: 55, power_watts: null, power_limit_watts: null }],
    });
    expect(gpu).toMatchObject({ name: "RTX 4070", utilization_percent: 40, memory_used_bytes: 1024, power_watts: null });
  });
});
