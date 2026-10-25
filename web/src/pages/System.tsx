import { getModelsStatus, getStatus } from "../api/endpoints";
import type { BackendState as State, GpuStats } from "../api/types";
import { BackendState } from "../components/BackendState";
import { EmptyState, ErrorBanner, Meter, PageHeader, Stat } from "../components/ui";
import { formatBytes, formatDuration, formatInt } from "../lib/format";
import { usePolling } from "../lib/usePolling";

const BACKEND_NOTES: Record<State, string> = {
  ready: "Ollama is running and answering. It scales back to zero after the idle period with no requests.",
  waking: "A request raised demand; KEDA is starting the Ollama pod. Model loading follows, usually 30–90 s.",
  sleeping: "Scaled to zero, GPU memory is free. The next request wakes it; KEDA polls demand every 5 s.",
};

function idleSub(backend: State, idleFor: number | null, idleSeconds: number | null): string {
  if (backend !== "ready" || idleFor === null || idleSeconds === null) return "since the last request";
  const left = idleSeconds - idleFor;
  return left > 0 ? `idle timeout in ${formatDuration(left)}` : "idle timeout reached, scaling down";
}

export function SystemPage() {
  const status = usePolling((signal) => getStatus(signal), 2000);
  const models = usePolling((signal) => getModelsStatus(signal), 5000);
  const s = status.data;
  const queued = s ? s.queue.autocomplete + s.queue.chat + s.queue.batch : 0;

  return (
    <div className="page">
      <PageHeader
        title="System"
        description="Live state of the inference backend, the GPU scheduler and the GPU. Refreshes every 2 seconds."
        actions={s && <BackendState state={s.backend} />}
      />
      <ErrorBanner error={status.error} onRetry={status.refresh} />

      {s && (
        <>
          <div className="banner banner-plain">{BACKEND_NOTES[s.backend]}</div>

          <h2 className="section-title">Scheduler</h2>
          <div className="stats">
            <Stat
              label="In flight"
              value={s.max_inflight !== null ? `${formatInt(s.inflight)} / ${formatInt(s.max_inflight)}` : formatInt(s.inflight)}
              sub="GPU slots in use"
            />
            <Stat label="Queued" value={formatInt(queued)} sub="all priorities" />
            <Stat label="Autocomplete" value={formatInt(s.queue.autocomplete)} sub="priority 0, runs first" />
            <Stat label="Chat" value={formatInt(s.queue.chat)} sub="priority 1" />
            <Stat label="Batch" value={formatInt(s.queue.batch)} sub="priority 2, embeddings and indexing" />
            <Stat label="Demand" value={formatInt(s.demand)} sub="KEDA scaling signal" />
            <Stat label="Idle for" value={formatDuration(s.idle_for_s)} sub={idleSub(s.backend, s.idle_for_s, s.idle_seconds)} />
          </div>

          <h2 className="section-title">GPU</h2>
          {s.gpu ? (
            <GpuPanel gpu={s.gpu} />
          ) : (
            <EmptyState title="No GPU data">
              The gateway could not reach gpu-exporter. Check the <code>gpu-exporter</code> pod in the{" "}
              <code>forge-inference</code> namespace: <code>kubectl -n forge-inference logs deploy/gpu-exporter</code>.
            </EmptyState>
          )}
        </>
      )}
      {!s && status.loading && <div className="muted small pad">Loading status…</div>}

      <h2 className="section-title">Models</h2>
      <ErrorBanner error={models.error} onRetry={models.refresh} />
      {models.data && models.data.models.length === 0 && (
        <EmptyState title="No models configured">
          Models come from <code>gitops/models.yaml</code>. Add an entry and merge it; Argo CD restarts Ollama, which pulls it.
        </EmptyState>
      )}
      {models.data && models.data.models.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Alias</th>
                <th>Ollama model</th>
                <th>Role</th>
                <th>Pulled</th>
                <th>Loaded</th>
                <th className="num">VRAM</th>
              </tr>
            </thead>
            <tbody>
              {models.data.models.map((m) => (
                <tr key={m.alias}>
                  <td className="mono">{m.alias}</td>
                  <td className="mono muted">{m.name}</td>
                  <td>{m.role}</td>
                  <td>
                    {m.pulled === null ? (
                      <span className="muted" title="Not checked while the backend sleeps">–</span>
                    ) : m.pulled ? (
                      "yes"
                    ) : (
                      <span className="text-warn">not pulled</span>
                    )}
                  </td>
                  <td>{m.loaded ? <span className="text-ok">loaded</span> : <span className="muted">no</span>}</td>
                  <td className="num mono">{m.loaded ? formatBytes(m.size_vram) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function GpuPanel({ gpu }: { gpu: GpuStats }) {
  const util = gpu.utilization_percent;
  const used = gpu.memory_used_bytes;
  const total = gpu.memory_total_bytes;
  const power = gpu.power_watts;
  const limit = gpu.power_limit_watts;

  return (
    <div className="gpu">
      <div className="gpu-name mono">{gpu.name ?? "GPU"}</div>
      <dl className="gauges">
        <div className="gauge">
          <dt>Utilisation</dt>
          <dd>
            <Meter value={util ?? 0} max={100} warnAt={0.95} />
            <span className="mono">{util !== null ? `${Math.round(util)}%` : "–"}</span>
          </dd>
        </div>
        <div className="gauge">
          <dt>VRAM</dt>
          <dd>
            <Meter value={used ?? 0} max={total ?? 0} warnAt={0.9} />
            <span className="mono">
              {formatBytes(used)} / {formatBytes(total)}
            </span>
          </dd>
        </div>
        <div className="gauge">
          <dt>Power</dt>
          <dd>
            <Meter value={power ?? 0} max={limit ?? 0} warnAt={0.95} />
            <span className="mono">
              {power !== null ? `${power.toFixed(0)} W` : "–"}
              {limit !== null && ` / ${limit.toFixed(0)} W`}
            </span>
          </dd>
        </div>
        <div className="gauge">
          <dt>Temperature</dt>
          <dd>
            <Meter value={gpu.temperature_celsius ?? 0} max={100} warnAt={0.83} />
            <span className="mono">{gpu.temperature_celsius !== null ? `${Math.round(gpu.temperature_celsius)} °C` : "–"}</span>
          </dd>
        </div>
      </dl>
    </div>
  );
}
