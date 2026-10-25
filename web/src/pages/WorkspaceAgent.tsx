/**
 * Agent tab of the editor: chat with the model, which reads, searches and changes files.
 * Protocol, changeset and safety rules live in lib/agent.ts; this file is the loop and the UI.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, describeError } from "../api/client";
import { listOpenAIModels, workspaces } from "../api/endpoints";
import { streamChat } from "../api/stream";
import type { ChatMessage, OpenAIModel, WorkspaceEntry } from "../api/types";
import { DiffView } from "../components/DiffView";
import { Markdown } from "../components/Markdown";
import {
  AGENT_SYSTEM_PROMPT,
  changeCount,
  describeProject,
  emptyChangeset,
  narrative,
  needsFollowUp,
  parseActions,
  runActions,
  type AgentIO,
  type Changeset,
  type StepLog,
} from "../lib/agent";
import { diffLines, diffStats, foldUnchanged } from "../lib/diff";
import { formatInt } from "../lib/format";
import { useStoredState } from "../lib/useStoredState";

/** Read/search rounds per message before the agent must answer with what it has. */
const MAX_STEPS = 5;
/** Characters of prompt we allow: about 5k tokens, leaving room for the answer in 8k. */
const PROMPT_BUDGET = 17_000;
const OPEN_FILE_MAX = 6_000;

interface Step {
  text: string;
  log: StepLog[];
}

type Turn = { role: "user"; text: string } | { role: "agent"; steps: Step[]; live?: string; writing?: string; error?: string };

/** Live view of a streaming answer: hides finished blocks and cuts at an unfinished one. */
function liveView(text: string): { text: string; writing?: string } {
  const opener = /(?:<|```)(write|edit)\s+path\s*=\s*"([^"\n]*)"\s*>/g;
  let m: RegExpExecArray | null;
  while ((m = opener.exec(text))) {
    if (!text.includes(`</${m[1]}>`, m.index)) return { text: narrative(text.slice(0, m.index)), writing: m[2] };
  }
  return { text: narrative(text) };
}

function fitBudget(messages: ChatMessage[]): ChatMessage[] {
  // Drop the oldest history (never the system prompt or the current task) until it fits.
  const out = [...messages];
  const size = () => out.reduce((n, m) => n + m.content.length, 0);
  while (size() > PROMPT_BUDGET && out.length > 3) out.splice(1, 1);
  return out;
}

export function AgentPanel({
  name,
  entries,
  openFile,
  onApplied,
}: {
  name: string;
  entries: WorkspaceEntry[] | null;
  openFile: { path: string; content: string } | null;
  /** path -> new content (null = deleted), plus whether folders changed. */
  onApplied: (files: Map<string, string | null>) => void;
}) {
  const [models, setModels] = useState<OpenAIModel[]>([]);
  const [model, setModel] = useStoredState<string>("forge.chat.model", "");
  const [autoApply, setAutoApply] = useStoredState<"on" | "off">("forge.agent.autoApply", "off");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [changes, setChanges] = useState<Changeset>(emptyChangeset);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  const history = useRef<ChatMessage[]>([]);
  const changesRef = useRef<Changeset>(changes);
  const abortRef = useRef<AbortController | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  // Latest props for the async loop.
  const live = useRef({ entries, openFile });
  live.current = { entries, openFile };

  useEffect(() => {
    const c = new AbortController();
    listOpenAIModels(c.signal)
      .then((l) => setModels(l.filter((m) => !m.role || m.role === "chat")))
      .catch(() => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => {
    if (models.length > 0 && !models.some((m) => m.id === model)) setModel(models[0]!.id);
  }, [models, model, setModel]);
  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const updateAgent = (patch: (t: Extract<Turn, { role: "agent" }>) => Extract<Turn, { role: "agent" }>) =>
    setTurns((ts) => {
      const last = ts[ts.length - 1];
      return last && last.role === "agent" ? [...ts.slice(0, -1), patch(last)] : ts;
    });

  const io: AgentIO = {
    read: async (path) => {
      const open = live.current.openFile;
      if (open && open.path === path) return open.content;
      try {
        return (await workspaces.read(name, path)).content;
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
    isDir: (path) =>
      changesRef.current.folders.has(path) || (live.current.entries ?? []).some((e) => e.type === "dir" && e.path === path),
    search: async (query) => (await workspaces.search(name, query)).hits,
  };

  const apply = useCallback(
    async (keys: Set<string>) => {
      const cs = changesRef.current;
      setApplying(true);
      setApplyError(null);
      const done = new Map<string, string | null>();
      const errors: string[] = [];
      const attempt = async (key: string, fn: () => Promise<unknown>) => {
        try {
          await fn();
          return true;
        } catch (err) {
          errors.push(`${key.slice(key.indexOf(":") + 1)}: ${describeError(err)}`);
          return false;
        }
      };
      for (const f of cs.folders) {
        const key = `mkdir:${f}`;
        if (keys.has(key) && (await attempt(key, () => workspaces.mkdir(name, f).catch((e) => (e instanceof ApiError && e.status === 409 ? null : Promise.reject(e))))))
          cs.folders.delete(f);
      }
      for (const [path, change] of [...cs.files]) {
        const key = `file:${path}`;
        if (!keys.has(key) || change.after === null) continue;
        if (await attempt(key, () => workspaces.write(name, path, change.after!))) {
          done.set(path, change.after);
          cs.files.delete(path);
        }
      }
      for (const [path, change] of [...cs.files]) {
        const key = `file:${path}`;
        if (!keys.has(key) || change.after !== null) continue;
        if (await attempt(key, () => workspaces.removeEntry(name, path))) {
          done.set(path, null);
          cs.files.delete(path);
        }
      }
      for (const d of cs.deletedFolders) {
        const key = `rmdir:${d}`;
        if (keys.has(key) && (await attempt(key, () => workspaces.removeEntry(name, d)))) {
          cs.deletedFolders.delete(d);
          done.set(`${d}/`, null);
        }
      }
      const next = { files: new Map(cs.files), folders: new Set(cs.folders), deletedFolders: new Set(cs.deletedFolders) };
      changesRef.current = next;
      setChanges(next);
      setSelected(new Set());
      setApplying(false);
      if (errors.length) setApplyError(errors.join("\n"));
      onApplied(done);
    },
    [name, onApplied],
  );

  const run = async () => {
    const text = input.trim();
    if (!text || running || !model) return;
    setInput("");
    setRunning(true);
    setApplyError(null);
    setTurns((ts) => [...ts, { role: "user", text }, { role: "agent", steps: [] }]);

    const cs = changesRef.current;
    const { entries: ents, openFile: open } = live.current;
    const paths = (ents ?? []).filter((e) => e.type === "file").map((e) => e.path);
    let context = describeProject(paths, cs);
    if (open) {
      context +=
        open.content.length <= OPEN_FILE_MAX
          ? `\n\nOpen in the editor:\n<file path="${open.path}">\n${open.content}\n</file>`
          : `\n\nOpen in the editor: ${open.path} (long; <read> it if you need it)`;
    }
    const messages: ChatMessage[] = fitBudget([
      { role: "system", content: AGENT_SYSTEM_PROMPT },
      ...history.current,
      { role: "user", content: `${context}\n\nTask: ${text}` },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    const summary: string[] = [];
    const proposed: string[] = [];
    try {
      for (let step = 0; step < MAX_STEPS; step++) {
        let answer = "";
        await streamChat({
          model,
          messages,
          signal: controller.signal,
          onDelta: (d) => {
            answer += d;
            const v = liveView(answer);
            updateAgent((t) => ({ ...t, live: v.text, writing: v.writing }));
          },
        });
        const actions = parseActions(answer);
        const { results, log } = await runActions(actions, cs, io);
        const said = narrative(answer);
        summary.push(said);
        proposed.push(...log.filter((l) => l.ok && !/^(read|search)/.test(l.label)).map((l) => l.label));
        updateAgent((t) => ({ ...t, steps: [...t.steps, { text: said, log }], live: undefined, writing: undefined }));
        messages.push({ role: "assistant", content: answer });
        if (!needsFollowUp(actions, results)) break;
        if (step === MAX_STEPS - 1) {
          updateAgent((t) => ({ ...t, error: `Stopped after ${MAX_STEPS} rounds of reading. Ask again to continue.` }));
          break;
        }
        messages.push({
          role: "user",
          content: `Results:\n${results.join("\n\n")}\n\nContinue the task. When it is done, reply with a short summary and no read or search blocks.`,
        });
      }
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      updateAgent((t) => ({ ...t, live: undefined, writing: undefined, error: aborted ? "Stopped." : describeError(err) }));
    } finally {
      abortRef.current = null;
      setRunning(false);
    }

    // Later turns get the gist, not the full file contents, to save context.
    history.current.push(
      { role: "user", content: text },
      { role: "assistant", content: `${summary.filter(Boolean).join("\n") || "(no reply)"}${proposed.length ? `\n(Proposed: ${proposed.join("; ")})` : ""}` },
    );
    const next = { files: new Map(cs.files), folders: new Set(cs.folders), deletedFolders: new Set(cs.deletedFolders) };
    changesRef.current = next;
    setChanges(next);
    const keys = changeKeys(next);
    setSelected(keys);
    if (autoApply === "on" && keys.size > 0) void apply(keys);
  };

  const discard = () => {
    const empty = emptyChangeset();
    changesRef.current = empty;
    setChanges(empty);
    setSelected(new Set());
    history.current.push({ role: "user", content: "(I discarded all proposed changes.)" }, { role: "assistant", content: "Understood." });
  };

  const newChat = () => {
    abortRef.current?.abort();
    history.current = [];
    setTurns([]);
    discard();
    history.current = [];
  };

  const count = changeCount(changes);

  return (
    <div className="agent">
      <div className="agent-thread" ref={threadRef}>
        {turns.length === 0 && (
          <div className="agent-empty muted small">
            <p>Describe what to build or change. The agent sees the file tree and the open file, can read and search the project, and proposes file changes for you to review.</p>
            <ul>
              <li>“Create a FastAPI app with a /health route, tests and a README”</li>
              <li>“Find where the config is loaded and add a DEBUG flag”</li>
              <li>“Split utils.py into one file per class”</li>
            </ul>
          </div>
        )}
        {turns.map((t, i) =>
          t.role === "user" ? (
            <div key={i} className="agent-user">
              {t.text}
            </div>
          ) : (
            <div key={i} className="agent-reply">
              {t.steps.map((s, j) => (
                <div key={j} className="agent-step">
                  {s.text && <Markdown content={s.text} />}
                  {s.log.length > 0 && (
                    <ul className="agent-log">
                      {s.log.map((l, k) => (
                        <li key={k} className={l.ok ? "" : "bad"}>
                          {l.label}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
              {t.live !== undefined && (
                <div className="agent-step">
                  {t.live && <Markdown content={t.live} />}
                  {t.writing !== undefined ? (
                    <div className="agent-writing mono small">
                      <span className="caret" aria-hidden="true" /> writing {t.writing}…
                    </div>
                  ) : (
                    <span className="caret" aria-hidden="true" />
                  )}
                </div>
              )}
              {running && i === turns.length - 1 && t.live === undefined && (
                <div className="muted small">
                  <span className="caret" aria-hidden="true" /> thinking…
                </div>
              )}
              {t.error && <div className="turn-error small">{t.error}</div>}
            </div>
          ),
        )}
      </div>

      {count > 0 && (
        <ChangeReview
          changes={changes}
          selected={selected}
          onToggle={(key) =>
            setSelected((s) => {
              const next = new Set(s);
              if (next.has(key)) next.delete(key);
              else next.add(key);
              return next;
            })
          }
          onApply={() => void apply(selected)}
          onDiscard={discard}
          busy={applying || running}
        />
      )}
      {applyError && <div className="banner banner-error small agent-apply-error">{applyError}</div>}

      <form
        className="agent-composer"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <textarea
          rows={3}
          value={input}
          placeholder="Ask the agent to build or change something…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void run();
            }
          }}
        />
        <div className="agent-bar">
          <select value={model} onChange={(e) => setModel(e.target.value)} disabled={running} aria-label="Model">
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}
              </option>
            ))}
          </select>
          <label className="agent-auto small" title="Apply the agent's changes without review">
            <input type="checkbox" checked={autoApply === "on"} onChange={(e) => setAutoApply(e.target.checked ? "on" : "off")} /> auto-apply
          </label>
          <button type="button" className="btn btn-small" onClick={newChat} disabled={running || turns.length === 0}>
            New
          </button>
          {running ? (
            <button type="button" className="btn btn-small" onClick={() => abortRef.current?.abort()}>
              Stop
            </button>
          ) : (
            <button type="submit" className="btn btn-small btn-primary" disabled={!input.trim() || !model}>
              Send
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

function changeKeys(cs: Changeset): Set<string> {
  return new Set([
    ...[...cs.folders].map((f) => `mkdir:${f}`),
    ...[...cs.files.keys()].map((p) => `file:${p}`),
    ...[...cs.deletedFolders].map((d) => `rmdir:${d}`),
  ]);
}

function ChangeReview({
  changes,
  selected,
  onToggle,
  onApply,
  onDiscard,
  busy,
}: {
  changes: Changeset;
  selected: Set<string>;
  onToggle: (key: string) => void;
  onApply: () => void;
  onDiscard: () => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const rows: { key: string; badge: string; cls: string; path: string; stats?: string; diff?: () => ReturnType<typeof foldUnchanged> }[] = [
    ...[...changes.folders].map((f) => ({ key: `mkdir:${f}`, badge: "dir", cls: "new", path: `${f}/` })),
    ...[...changes.files.values()].map((c) => {
      const lines = diffLines(c.before ?? "", c.after ?? "");
      const s = diffStats(lines);
      return {
        key: `file:${c.path}`,
        badge: c.before === null ? "new" : c.after === null ? "del" : "edit",
        cls: c.before === null ? "new" : c.after === null ? "del" : "mod",
        path: c.path,
        stats: c.after === null ? `−${s.removed}` : `+${s.added} −${s.removed}`,
        diff: () => foldUnchanged(lines),
      };
    }),
    ...[...changes.deletedFolders].map((d) => ({ key: `rmdir:${d}`, badge: "del", cls: "del", path: `${d}/` })),
  ];
  return (
    <div className="review">
      <div className="review-head">
        <span className="section-label">
          {rows.length} proposed change{rows.length === 1 ? "" : "s"}
        </span>
        <span>
          <button type="button" className="btn btn-small btn-primary" onClick={onApply} disabled={busy || selected.size === 0}>
            Apply {selected.size === rows.length ? "all" : formatInt(selected.size)}
          </button>
          <button type="button" className="btn btn-small" onClick={onDiscard} disabled={busy}>
            Discard
          </button>
        </span>
      </div>
      <ul className="review-list">
        {rows.map((r) => (
          <li key={r.key}>
            <div className="review-row">
              <input type="checkbox" checked={selected.has(r.key)} onChange={() => onToggle(r.key)} aria-label={`Include ${r.path}`} />
              <span className={`review-badge git-${r.cls}`}>{r.badge}</span>
              {r.diff ? (
                <button type="button" className="link-button mono review-path" onClick={() => setOpen(open === r.key ? null : r.key)} aria-expanded={open === r.key}>
                  {r.path}
                </button>
              ) : (
                <span className="mono review-path">{r.path}</span>
              )}
              {r.stats && <span className="mono small muted">{r.stats}</span>}
            </div>
            {open === r.key && r.diff && <DiffView rows={r.diff()} />}
          </li>
        ))}
      </ul>
    </div>
  );
}
