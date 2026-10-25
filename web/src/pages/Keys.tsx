import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { keys, type KeyLimits } from "../api/endpoints";
import type { ApiKey, CreatedApiKey } from "../api/types";
import { CopyButton, EmptyState, ErrorBanner, PageHeader } from "../components/ui";
import { formatCompact, formatDateTime, formatInt, formatRelative } from "../lib/format";
import { LIMIT_BOUNDS, parseLimit } from "../lib/limits";
import { usePolling } from "../lib/usePolling";

export function KeysPage() {
  const list = usePolling(() => keys.list(), null);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);

  const all = list.data ?? [];
  const visible = all.filter((k) => showRevoked || !k.revoked_at);
  const revokedCount = all.filter((k) => k.revoked_at).length;

  return (
    <div className="page">
      <PageHeader
        title="API keys"
        description={
          <>
            Keys authenticate <code>/v1</code> calls from other projects and VS Code. Each key has its own rate limit and
            daily token quota. See <Link to="/connect">Connect</Link> for client setup.
          </>
        }
      />

      {created && <NewKeyNotice created={created} onDismiss={() => setCreated(null)} />}

      <CreateKeyForm
        onCreated={(k) => {
          setCreated(k);
          list.refresh();
        }}
      />

      <ErrorBanner error={list.error} onRetry={list.refresh} />

      <div className="table-head">
        <span className="section-label">
          {formatInt(all.length - revokedCount)} active{revokedCount > 0 && `, ${formatInt(revokedCount)} revoked`}
        </span>
        {revokedCount > 0 && (
          <label className="check">
            <input type="checkbox" checked={showRevoked} onChange={(e) => setShowRevoked(e.target.checked)} /> show revoked
          </label>
        )}
      </div>

      {list.loading && !list.data ? (
        <div className="muted small pad">Loading keys…</div>
      ) : visible.length === 0 ? (
        <EmptyState title="No API keys yet">
          Create one above for each client (for example <code>vigil</code>, <code>pulse</code>, <code>vscode</code>), so
          usage and limits are tracked per project.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table table-stack">
            <thead>
              <tr>
                <th>Name</th>
                <th>Key</th>
                <th className="num">RPM</th>
                <th className="num">Daily tokens</th>
                <th>Created</th>
                <th>Last used</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {visible.map((k) => (
                <KeyRow key={k.id} apiKey={k} onChanged={list.refresh} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function NewKeyNotice({ created, onDismiss }: { created: CreatedApiKey; onDismiss: () => void }) {
  return (
    <div className="banner banner-accent new-key" role="status">
      <div className="new-key-head">
        <strong>Key created for {created.name}.</strong> Copy it now: Forge stores only a hash, so it cannot be shown again.
      </div>
      <div className="new-key-row">
        <code className="key-value">{created.key}</code>
        <CopyButton text={created.key} />
        <button type="button" className="btn btn-small" onClick={onDismiss}>
          Done
        </button>
      </div>
    </div>
  );
}

function CreateKeyForm({ onCreated }: { onCreated: (k: CreatedApiKey) => void }) {
  const [name, setName] = useState("");
  const [rpm, setRpm] = useState("");
  const [quota, setQuota] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    // Blank means "use the default"; anything else must be a valid limit, never silently dropped.
    const r = parseLimit(rpm, LIMIT_BOUNDS.rpm);
    const q = parseLimit(quota, LIMIT_BOUNDS.dailyTokens);
    const invalid = [r.ok ? null : `Requests / min: ${r.error}`, q.ok ? null : `Daily tokens: ${q.error}`].filter(Boolean);
    if (invalid.length > 0) {
      setError(new Error(invalid.join(" ")));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const limits: KeyLimits = {};
      if (r.ok && r.value !== null) limits.rpm_limit = r.value;
      if (q.ok && q.value !== null) limits.daily_token_quota = q.value;
      onCreated(await keys.create(name.trim(), limits));
      setName("");
      setRpm("");
      setQuota("");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form-row" onSubmit={(e) => void onSubmit(e)}>
      <label className="field">
        <span className="field-label">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="vigil" required maxLength={64} />
      </label>
      <label className="field field-narrow">
        <span className="field-label">Requests / min</span>
        <input inputMode="numeric" value={rpm} onChange={(e) => setRpm(e.target.value)} placeholder="default" />
      </label>
      <label className="field field-narrow">
        <span className="field-label">Daily tokens</span>
        <input inputMode="numeric" value={quota} onChange={(e) => setQuota(e.target.value)} placeholder="default" />
      </label>
      <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
        {busy ? "Creating…" : "Create key"}
      </button>
      <ErrorBanner error={error} />
    </form>
  );
}

function KeyRow({ apiKey: k, onChanged }: { apiKey: ApiKey; onChanged: () => void }) {
  const [mode, setMode] = useState<"view" | "edit" | "confirm-revoke">("view");
  const [rpm, setRpm] = useState(String(k.rpm_limit));
  const [quota, setQuota] = useState(String(k.daily_token_quota));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const revoked = k.revoked_at !== null;
  const limited = k.limited !== false;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setMode("view");
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const r = parseLimit(rpm, LIMIT_BOUNDS.rpm);
    const q = parseLimit(quota, LIMIT_BOUNDS.dailyTokens);
    const rpmLimit = r.ok ? r.value : null;
    const dailyQuota = q.ok ? q.value : null;
    if (rpmLimit === null || dailyQuota === null) {
      const rError = r.ok ? "required." : r.error;
      const qError = q.ok ? "required." : q.error;
      const parts = [rpmLimit === null && `Requests / min: ${rError}`, dailyQuota === null && `Daily tokens: ${qError}`];
      setError(new Error(parts.filter(Boolean).join(" ")));
      return;
    }
    void run(() => keys.update(k.id, { rpm_limit: rpmLimit, daily_token_quota: dailyQuota }));
  };

  return (
    <>
      <tr className={revoked ? "row-muted" : ""}>
        <td className="cell-title">
          {k.name}
          {k.builtin && <span className="tag">built-in</span>}
          {revoked && <span className="tag tag-failed">revoked</span>}
        </td>
        <td className="mono" data-label="Key">
          {k.prefix ? `${k.prefix}…` : <span className="muted">session</span>}
        </td>
        {mode === "edit" ? (
          <>
            <td className="num" data-label="RPM">
              <input className="cell-input" inputMode="numeric" value={rpm} onChange={(e) => setRpm(e.target.value)} aria-label="Requests per minute" />
            </td>
            <td className="num" data-label="Daily tokens">
              <input className="cell-input" inputMode="numeric" value={quota} onChange={(e) => setQuota(e.target.value)} aria-label="Daily token quota" />
            </td>
          </>
        ) : !limited ? (
          <td colSpan={2} className="num muted" data-label="Limits" title="The admin session is metered but never limited">
            not limited
          </td>
        ) : (
          <>
            <td className="num mono" data-label="RPM">
              {formatInt(k.rpm_limit)}
            </td>
            <td className="num mono" data-label="Daily tokens" title={formatInt(k.daily_token_quota)}>
              {formatCompact(k.daily_token_quota)}
            </td>
          </>
        )}
        <td data-label="Created" title={formatDateTime(k.created_at)}>
          {formatRelative(k.created_at)}
        </td>
        <td data-label="Last used" title={formatDateTime(k.last_used_at)}>
          {formatRelative(k.last_used_at)}
        </td>
        <td className="row-actions">
          {revoked ? (
            <span className="muted small">{formatRelative(k.revoked_at)}</span>
          ) : mode === "edit" ? (
            <>
              <button type="button" className="btn btn-small btn-primary" onClick={save} disabled={busy}>
                Save
              </button>
              <button type="button" className="btn btn-small" onClick={() => setMode("view")} disabled={busy}>
                Cancel
              </button>
            </>
          ) : mode === "confirm-revoke" ? (
            <>
              <button type="button" className="btn btn-small btn-danger" onClick={() => void run(() => keys.revoke(k.id))} disabled={busy}>
                Revoke now
              </button>
              <button type="button" className="btn btn-small" onClick={() => setMode("view")} disabled={busy}>
                Cancel
              </button>
            </>
          ) : (
            <>
              {limited && (
                <button
                  type="button"
                  className="btn btn-small"
                  onClick={() => {
                    setRpm(String(k.rpm_limit));
                    setQuota(String(k.daily_token_quota));
                    setMode("edit");
                  }}
                >
                  Edit limits
                </button>
              )}
              {!k.builtin && (
                <button type="button" className="btn btn-small" onClick={() => setMode("confirm-revoke")}>
                  Revoke
                </button>
              )}
            </>
          )}
        </td>
      </tr>
      {error !== null && (
        <tr>
          <td colSpan={7}>
            <ErrorBanner error={error} />
          </td>
        </tr>
      )}
    </>
  );
}
