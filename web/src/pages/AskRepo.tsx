import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { describeError } from "../api/client";
import { repos } from "../api/endpoints";
import { streamRepoAnswer, type StreamStats } from "../api/stream";
import type { RagSource, Repo, RepoStatus } from "../api/types";
import { Markdown } from "../components/Markdown";
import { EmptyState, ErrorBanner, PageHeader } from "../components/ui";
import { formatInt, formatMs, formatRate, formatRelative } from "../lib/format";
import { repoName, sourceUrl } from "../lib/repoLinks";
import { usePolling } from "../lib/usePolling";

const ACTIVE: RepoStatus[] = ["queued", "cloning", "embedding"];

function statusText(repo: Repo): string {
  switch (repo.status) {
    case "queued":
      return "queued for indexing";
    case "cloning":
      return "cloning";
    case "embedding":
      return `embedding · ${formatInt(repo.files_indexed)} files · ${formatInt(repo.chunks)} chunks`;
    case "ready":
      return `${formatInt(repo.files_indexed)} files · ${formatInt(repo.chunks)} chunks`;
    case "failed":
      return repo.error ?? "indexing failed";
  }
}

export function AskRepoPage() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("repo") ? Number(params.get("repo")) : null;

  const [anyActive, setAnyActive] = useState(false);
  // Poll fast only while something is indexing.
  const list = usePolling(() => repos.list(), anyActive ? 2000 : 20_000);
  const repoList = useMemo(() => [...(list.data ?? [])].sort((a, b) => b.id - a.id), [list.data]);
  useEffect(() => setAnyActive(repoList.some((r) => ACTIVE.includes(r.status))), [repoList]);

  const selected = repoList.find((r) => r.id === selectedId) ?? null;
  const select = (id: number | null) => setParams(id === null ? {} : { repo: String(id) }, { replace: true });

  // Pick the most recent ready repo when none is selected, so the page is immediately usable.
  useEffect(() => {
    if (selectedId === null && repoList.length > 0) {
      const ready = repoList.find((r) => r.status === "ready") ?? repoList[0]!;
      setParams({ repo: String(ready.id) }, { replace: true });
    }
  }, [selectedId, repoList, setParams]);

  return (
    <div className="page page-split">
      <PageHeader
        title="Ask repo"
        description="Index a public repository, then ask questions answered only from its code, with cited sources."
      />
      <div className="split">
        <div className="split-left">
          <AddRepoForm
            onAdded={(repo) => {
              list.refresh();
              select(repo.id);
            }}
          />
          <ErrorBanner error={list.error} onRetry={list.refresh} />
          {list.loading && !list.data ? (
            <div className="muted small pad">Loading repositories…</div>
          ) : repoList.length === 0 ? (
            <EmptyState title="No repositories indexed">
              Paste an <code>https://</code> URL from GitHub, GitLab or Codeberg above. Indexing clones the default branch
              (or the one you name), embeds up to 3000 text files, and usually takes a few minutes.
            </EmptyState>
          ) : (
            <ul className="repo-list">
              {repoList.map((r) => (
                <li key={r.id}>
                  <button type="button" className={`repo-item ${r.id === selected?.id ? "active" : ""}`} onClick={() => select(r.id)}>
                    <span className="repo-name">{repoName(r.url)}</span>
                    <span className={`tag tag-${r.status}`}>{r.status}</span>
                    <span className="repo-meta">{statusText(r)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="split-right">
          {selected ? (
            <RepoPanel key={selected.id} repo={selected} onChanged={list.refresh} onDeleted={() => { select(null); list.refresh(); }} />
          ) : (
            repoList.length > 0 && <EmptyState title="Select a repository" />
          )}
        </div>
      </div>
    </div>
  );
}

function AddRepoForm({ onAdded }: { onAdded: (repo: Repo) => void }) {
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const repo = await repos.create(url.trim(), branch.trim() || null);
      setUrl("");
      setBranch("");
      onAdded(repo);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="add-repo" onSubmit={(e) => void onSubmit(e)}>
      <input
        type="url"
        placeholder="https://github.com/owner/name"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        pattern="https://.*"
        required
        aria-label="Repository URL"
      />
      <div className="add-repo-row">
        <input type="text" placeholder="branch (optional)" value={branch} onChange={(e) => setBranch(e.target.value)} aria-label="Branch" />
        <button type="submit" className="btn btn-primary" disabled={busy || !url.trim()}>
          {busy ? "Adding…" : "Index"}
        </button>
      </div>
      <ErrorBanner error={error} />
    </form>
  );
}

interface Answer {
  question: string;
  text: string;
  sources: RagSource[];
  stats?: StreamStats;
  error?: string;
  stopped?: boolean;
  pending: boolean;
}

function RepoPanel({ repo, onChanged, onDeleted }: { repo: Repo; onChanged: () => void; onDeleted: () => void }) {
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(8);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sourceRefs = useRef(new Map<number, HTMLLIElement>());

  useEffect(() => () => abortRef.current?.abort(), []);

  const ready = repo.status === "ready";
  const ref = repo.commit_sha ?? repo.branch;

  const ask = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = question.trim();
    if (!q || answer?.pending || !ready) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setHighlighted(null);
    setAnswer({ question: q, text: "", sources: [], pending: true });
    try {
      const result = await streamRepoAnswer({
        repoId: repo.id,
        question: q,
        topK,
        signal: controller.signal,
        onSources: (sources) => setAnswer((a) => (a ? { ...a, sources } : a)),
        onToken: (t) => setAnswer((a) => (a ? { ...a, text: a.text + t } : a)),
      });
      setAnswer((a) => (a ? { ...a, text: result.answer, sources: result.sources, stats: result.stats, pending: false } : a));
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      setAnswer((a) => (a ? { ...a, pending: false, ...(aborted ? { stopped: true } : { error: describeError(err) }) } : a));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const runAction = async (fn: () => Promise<unknown>, after: () => void) => {
    setActionError(null);
    try {
      await fn();
      after();
    } catch (err) {
      setActionError(err);
    }
  };

  const onCite = useCallback((n: number) => {
    setHighlighted(n);
    sourceRefs.current.get(n)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, []);
  const citations = useMemo(
    () => ({ valid: new Set((answer?.sources ?? []).map((s) => s.n)), onCite }),
    [answer?.sources, onCite],
  );

  return (
    <div className="repo-panel">
      <div className="repo-head">
        <div>
          <a href={repo.url} target="_blank" rel="noopener noreferrer" className="repo-title">
            {repoName(repo.url)}
          </a>
          <dl className="kv-inline">
            <dt>status</dt>
            <dd>
              <span className={`tag tag-${repo.status}`}>{repo.status}</span>
            </dd>
            <dt>branch</dt>
            <dd className="mono">{repo.branch ?? "default"}</dd>
            <dt>commit</dt>
            <dd className="mono">{repo.commit_sha ? repo.commit_sha.slice(0, 7) : "–"}</dd>
            <dt>files</dt>
            <dd className="mono">{formatInt(repo.files_indexed)}</dd>
            <dt>chunks</dt>
            <dd className="mono">{formatInt(repo.chunks)}</dd>
            <dt>updated</dt>
            <dd>{formatRelative(repo.updated_at)}</dd>
          </dl>
        </div>
        <div className="row-actions">
          <button
            type="button"
            className="btn btn-small"
            disabled={ACTIVE.includes(repo.status)}
            onClick={() => void runAction(() => repos.reindex(repo.id), onChanged)}
          >
            Re-index
          </button>
          {confirmDelete ? (
            <>
              <button type="button" className="btn btn-small btn-danger" onClick={() => void runAction(() => repos.remove(repo.id), onDeleted)}>
                Delete index
              </button>
              <button type="button" className="btn btn-small" onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" className="btn btn-small" onClick={() => setConfirmDelete(true)}>
              Delete
            </button>
          )}
        </div>
      </div>
      <ErrorBanner error={actionError} />
      {repo.status === "failed" && <div className="banner banner-error">{repo.error ?? "Indexing failed."} Re-index to try again.</div>}
      {ACTIVE.includes(repo.status) && (
        <div className="banner">
          Indexing: {statusText(repo)}. Questions open when the status is <span className="tag tag-ready">ready</span>.
        </div>
      )}

      <form className="ask" onSubmit={(e) => void ask(e)}>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) void ask(e);
            if (e.key === "Escape") abortRef.current?.abort();
          }}
          placeholder={ready ? "Where is the request retry logic implemented?" : "Waiting for the index to be ready"}
          rows={3}
          disabled={!ready}
        />
        <div className="composer-bar">
          <label className="inline-field">
            <span>Sources</span>
            <select value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
              {[4, 8, 12, 16].map((k) => (
                <option key={k} value={k}>
                  top {k}
                </option>
              ))}
            </select>
          </label>
          {answer?.pending ? (
            <button type="button" className="btn" onClick={() => abortRef.current?.abort()}>
              Stop
            </button>
          ) : (
            <button type="submit" className="btn btn-primary" disabled={!ready || !question.trim()}>
              Ask
            </button>
          )}
        </div>
      </form>

      {answer && (
        <div className="answer">
          <div className="answer-q">{answer.question}</div>
          {answer.text ? (
            <Markdown content={answer.text} citations={citations} />
          ) : answer.pending ? (
            <div className="waiting">
              <span className="caret" aria-hidden="true" />
              {answer.sources.length ? "Generating answer…" : "Retrieving sources…"}
            </div>
          ) : null}
          <div className="turn-foot">
            {answer.error && <span className="turn-error">{answer.error}</span>}
            {answer.stopped && <span className="muted">stopped</span>}
            {answer.stats && (
              <span className="mono muted">
                {formatInt(answer.stats.completionTokens)} tok · {formatRate(answer.stats.tokensPerSecond)} · ttft{" "}
                {formatMs(answer.stats.ttftMs)}
              </span>
            )}
          </div>

          {answer.sources.length > 0 && (
            <div className="sources">
              <div className="section-label">Sources</div>
              <ol className="source-list">
                {answer.sources.map((s) => {
                  const href = sourceUrl(repo.url, ref, s.path, s.start_line, s.end_line);
                  return (
                    <li
                      key={s.n}
                      ref={(el) => {
                        if (el) sourceRefs.current.set(s.n, el);
                        else sourceRefs.current.delete(s.n);
                      }}
                      className={highlighted === s.n ? "highlight" : ""}
                    >
                      <span className="source-n mono">[{s.n}]</span>
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer" className="mono">
                          {s.path}:{s.start_line}-{s.end_line}
                        </a>
                      ) : (
                        <span className="mono">
                          {s.path}:{s.start_line}-{s.end_line}
                        </span>
                      )}
                      <span className="source-score mono" title="Cosine similarity between the question and this chunk">
                        {typeof s.score === "number" ? s.score.toFixed(3) : ""}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
