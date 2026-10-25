import { useMemo } from "react";
import { Link } from "react-router-dom";
import { getUsage } from "../api/endpoints";
import type { UsageBreakdownRow, UsageRange } from "../api/types";
import { TimeSeriesChart } from "../components/TimeSeriesChart";
import { EmptyState, ErrorBanner, Meter, PageHeader, Stat } from "../components/ui";
import { fillBuckets } from "../lib/buckets";
import { formatCompact, formatInt, formatMs, formatPercent, formatRate } from "../lib/format";
import { usePolling } from "../lib/usePolling";
import { useStoredState } from "../lib/useStoredState";

const RANGES: { id: UsageRange; label: string; bucket: number; buckets: number }[] = [
  { id: "24h", label: "24 hours", bucket: 3600, buckets: 24 },
  { id: "7d", label: "7 days", bucket: 86_400, buckets: 7 },
  { id: "30d", label: "30 days", bucket: 86_400, buckets: 30 },
];

export function UsagePage() {
  const [rangeId, setRangeId] = useStoredState<UsageRange>("forge.usage.range", "24h");
  const range = RANGES.find((r) => r.id === rangeId) ?? RANGES[0]!;
  const usage = usePolling((signal) => getUsage(range.id, signal), 30_000, [range.id]);
  const data = usage.data;

  const series = useMemo(() => {
    const filled = fillBuckets(data?.timeseries ?? [], range.bucket, range.buckets, Date.now() / 1000);
    return { xs: filled.map((p) => p.t), requests: filled.map((p) => p.requests), tokens: filled.map((p) => p.tokens) };
  }, [data, range.bucket, range.buckets]);

  const totals = data?.totals;
  const lat = data?.latency;
  const errorRate = totals && totals.requests > 0 ? totals.errors / totals.requests : 0;

  return (
    <div className="page">
      <PageHeader
        title="Usage"
        description="Requests, tokens and latency across all keys, from the gateway's usage events."
        actions={
          <div className="segmented" role="tablist" aria-label="Time range">
            {RANGES.map((r) => (
              <button key={r.id} type="button" role="tab" aria-selected={r.id === range.id} className={r.id === range.id ? "active" : ""} onClick={() => setRangeId(r.id)}>
                {r.id}
              </button>
            ))}
          </div>
        }
      />
      <ErrorBanner error={usage.error} onRetry={usage.refresh} />

      {!data && usage.loading ? (
        <div className="muted small pad">Loading usage…</div>
      ) : data ? (
        <>
          <div className="stats">
            <Stat label="Requests" value={formatInt(totals?.requests)} sub={`last ${range.label}`} />
            <Stat label="Prompt tokens" value={formatCompact(totals?.prompt_tokens)} />
            <Stat label="Completion tokens" value={formatCompact(totals?.completion_tokens)} />
            <Stat
              label="Errors"
              value={formatInt(totals?.errors)}
              sub={totals && totals.requests > 0 ? `${formatPercent(errorRate)} of requests` : undefined}
            />
            <Stat label="Latency p50 / p95" value={`${formatMs(lat?.p50_ms)} / ${formatMs(lat?.p95_ms)}`} />
            <Stat label="TTFT p50 / p95" value={`${formatMs(lat?.ttft_p50_ms)} / ${formatMs(lat?.ttft_p95_ms)}`} />
            <Stat label="Generation speed" value={formatRate(lat?.tokens_per_s_avg)} sub="average after first token" />
          </div>

          {totals && totals.requests === 0 ? (
            <EmptyState title={`No requests in the last ${range.label}`}>
              Send a message in <Link to="/chat">Chat</Link>, or point a client at Forge with a key from{" "}
              <Link to="/keys">API keys</Link>. Numbers here update every 30 seconds.
            </EmptyState>
          ) : (
            <>
              <div className="panels">
                <section className="panel">
                  <h2 className="panel-title">Requests per {range.bucket === 3600 ? "hour" : "day"}</h2>
                  <TimeSeriesChart xs={series.xs} ys={series.requests} label="requests" format={formatCompact} bucketSeconds={range.bucket} kind="bars" />
                </section>
                <section className="panel">
                  <h2 className="panel-title">Tokens per {range.bucket === 3600 ? "hour" : "day"}</h2>
                  <TimeSeriesChart xs={series.xs} ys={series.tokens} label="tokens" format={formatCompact} bucketSeconds={range.bucket} kind="line" />
                </section>
              </div>

              <div className="panels">
                <section className="panel">
                  <h2 className="panel-title">By key</h2>
                  <BreakdownTable rows={data.by_key} labelHeader="Key" />
                </section>
                <section className="panel">
                  <h2 className="panel-title">By model</h2>
                  <BreakdownTable rows={data.by_model} labelHeader="Model" />
                </section>
              </div>
            </>
          )}

          <section className="panel">
            <h2 className="panel-title">Today's token quota</h2>
            {data.today_by_key.length === 0 ? (
              <div className="muted small pad">No key has used tokens today.</div>
            ) : (
              <table className="table quota-table">
                <tbody>
                  {[...data.today_by_key]
                    .sort((a, b) => b.used / (b.quota || 1) - a.used / (a.quota || 1))
                    .map((q) => (
                      <tr key={q.label}>
                        <td className="quota-name">{q.label}</td>
                        <td className="quota-bar">
                          <Meter value={q.used} max={q.quota} />
                        </td>
                        <td className="num mono">
                          {formatCompact(q.used)} / {formatCompact(q.quota)}
                        </td>
                        <td className="num mono muted">{q.quota > 0 ? formatPercent(q.used / q.quota) : "–"}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}

function BreakdownTable({ rows, labelHeader }: { rows: UsageBreakdownRow[]; labelHeader: string }) {
  if (rows.length === 0) return <div className="muted small pad">No traffic in this range.</div>;
  const sorted = [...rows].sort((a, b) => b.prompt_tokens + b.completion_tokens - (a.prompt_tokens + a.completion_tokens));
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>{labelHeader}</th>
            <th className="num">Requests</th>
            <th className="num">Prompt</th>
            <th className="num">Completion</th>
            <th className="num">Errors</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.label}>
              <td>{r.label}</td>
              <td className="num mono">{formatInt(r.requests)}</td>
              <td className="num mono">{formatCompact(r.prompt_tokens)}</td>
              <td className="num mono">{formatCompact(r.completion_tokens)}</td>
              <td className={`num mono ${r.errors > 0 ? "text-error" : "muted"}`}>{formatInt(r.errors)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
