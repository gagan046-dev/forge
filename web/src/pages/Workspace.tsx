import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { describeError } from "../api/client";
import { listOpenAIModels, workspaces } from "../api/endpoints";
import { streamChat } from "../api/stream";
import type { GitStatus, OpenAIModel, Workspace, WorkspaceEntry } from "../api/types";
import { CodeEditor } from "../components/CodeEditor";
import { DiffView } from "../components/DiffView";
import { EmptyState, ErrorBanner, useDocumentTitle } from "../components/ui";
import { diffLines, diffStats, extractCode, foldUnchanged } from "../lib/diff";
import { formatInt, formatRelative } from "../lib/format";
import { useStoredState } from "../lib/useStoredState";
import { AgentPanel } from "./WorkspaceAgent";

const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
/** Past this the 8k-token chat model can't hold the file, the instruction and a full rewrite. */
const AI_FILE_WARN_CHARS = 12_000;

interface OpenFile {
  path: string;
  saved: string;
}

export function WorkspacePage() {
  const { name } = useParams();
  const navigate = useNavigate();
  useDocumentTitle(name ? `${name} · Workspace` : "Workspace");

  const [list, setList] = useState<Workspace[] | null>(null);
  const [listError, setListError] = useState<unknown>(null);
  const [creating, setCreating] = useState(false);

  const refreshList = useCallback(() => {
    workspaces
      .list()
      .then((l) => {
        setList(l);
        setListError(null);
      })
      .catch(setListError);
  }, []);
  useEffect(refreshList, [refreshList]);

  // No workspace in the URL: open the first one if there is any.
  useEffect(() => {
    if (!name && list && list.length > 0 && !creating) navigate(`/workspace/${list[0]!.name}`, { replace: true });
  }, [name, list, creating, navigate]);

  // A workspace in the URL that no longer exists (deleted here or in another tab): leave it.
  useEffect(() => {
    if (name && list && !list.some((w) => w.name === name)) navigate("/workspace", { replace: true });
  }, [name, list, navigate]);

  const current = list?.find((w) => w.name === name) ?? null;

  return (
    <div className="ws">
      <div className="ws-top">
        <label className="inline-field">
          <span>Workspace</span>
          <select
            value={name ?? ""}
            onChange={(e) => navigate(`/workspace/${e.target.value}`)}
            disabled={!list || list.length === 0}
          >
            {!name && <option value="">—</option>}
            {(list ?? []).map((w) => (
              <option key={w.name} value={w.name}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn btn-small" onClick={() => setCreating((c) => !c)} aria-expanded={creating}>
          {creating ? "Cancel" : "New workspace"}
        </button>
        {current && (
          <span className="muted small mono ws-origin" title={current.git.remote ?? undefined}>
            {current.git.remote ? `${current.git.remote}${current.git.branch ? ` @ ${current.git.branch}` : ""}` : "local folder"} ·
            created {formatRelative(current.created_at)}
          </span>
        )}
        {name && (
          <span className="ws-top-actions">
            <a className="btn btn-small" href={workspaces.archiveUrl(name)} download={`${name}.zip`}>
              Download ZIP
            </a>
            <DeleteWorkspace
              name={name}
              onDeleted={() => {
                // Drop it from the list first: the "open the first workspace" effect would
                // otherwise reopen the deleted one from the stale list.
                setList((l) => l?.filter((w) => w.name !== name) ?? l);
                navigate("/workspace", { replace: true });
                refreshList();
              }}
            />
          </span>
        )}
      </div>
      <ErrorBanner error={listError} onRetry={refreshList} />
      {creating && (
        <CreateWorkspace
          onCreated={(w) => {
            setCreating(false);
            setList((l) => [...(l ?? []).filter((x) => x.name !== w.name), w].sort((a, b) => a.name.localeCompare(b.name)));
            navigate(`/workspace/${w.name}`);
            refreshList();
          }}
        />
      )}
      {list && list.length === 0 && !creating && (
        <EmptyState title="No workspaces yet">
          A workspace is a project folder on the server. Start an empty one, or clone a repository from GitHub, GitLab or
          Codeberg, then edit it here, let the model change files, download it as a ZIP or commit and push.
          <div className="pad-top">
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              New workspace
            </button>
          </div>
        </EmptyState>
      )}
      {name && current && <WorkspaceEditor key={name} name={name} onGitChanged={refreshList} />}
    </div>
  );
}

function CreateWorkspace({ onCreated }: { onCreated: (w: Workspace) => void }) {
  const [name, setName] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // A clone gets its name from the repository unless one was typed.
  const suggested = useMemo(() => {
    const last = gitUrl.trim().replace(/\/+$/, "").split("/").pop() ?? "";
    return last.replace(/\.git$/, "").toLowerCase().replace(/[^a-z0-9._-]/g, "-").replace(/^[^a-z0-9]+/, "").slice(0, 64);
  }, [gitUrl]);
  const finalName = name.trim() || suggested;
  const validName = NAME_RE.test(finalName);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!validName) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await workspaces.create(finalName, gitUrl.trim() || null, branch.trim() || null));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="panel ws-create" onSubmit={(e) => void submit(e)}>
      <div className="ws-create-grid">
        <label className="field">
          <span className="field-label">Git URL (optional)</span>
          <input
            type="url"
            className="mono"
            placeholder="https://github.com/owner/repo"
            value={gitUrl}
            onChange={(e) => setGitUrl(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">Branch</span>
          <input className="mono" placeholder="default" value={branch} onChange={(e) => setBranch(e.target.value)} disabled={!gitUrl.trim()} />
        </label>
        <label className="field">
          <span className="field-label">Name</span>
          <input className="mono" placeholder={suggested || "my-project"} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy || !validName}>
          {busy ? (gitUrl.trim() ? "Cloning…" : "Creating…") : gitUrl.trim() ? "Clone" : "Create"}
        </button>
      </div>
      <p className="muted small">
        Lower-case letters, digits, <code>.</code> <code>_</code> <code>-</code>. Clones are shallow (latest commit only); public
        repositories need no token. Private ones: create empty, then set the remote and push with a token.
      </p>
      <ErrorBanner error={error} />
    </form>
  );
}

function DeleteWorkspace({ name, onDeleted }: { name: string; onDeleted: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!confirming)
    return (
      <button type="button" className="btn btn-small btn-danger" onClick={() => setConfirming(true)}>
        Delete
      </button>
    );
  return (
    <>
      <button
        type="button"
        className="btn btn-small btn-danger"
        autoFocus
        onBlur={() => setConfirming(false)}
        onClick={() => {
          workspaces
            .remove(name)
            .then(onDeleted)
            .catch((err: unknown) => setError(describeError(err)));
        }}
      >
        Delete {name} and its files?
      </button>
      {error && <span className="turn-error small">{error}</span>}
    </>
  );
}

// ---------------------------------------------------------------------------- editor

function WorkspaceEditor({ name, onGitChanged }: { name: string; onGitChanged: () => void }) {
  const [entries, setEntries] = useState<WorkspaceEntry[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [treeError, setTreeError] = useState<unknown>(null);
  const [file, setFile] = useState<OpenFile | null>(null);
  const [editorValue, setEditorValue] = useState("");
  const [draft, setDraft] = useState("");
  const [fileError, setFileError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const [side, setSide] = useStoredState<"agent" | "ai" | "git">("forge.ws.side", "agent");
  const [gitTick, setGitTick] = useState(0);

  const dirty = file !== null && draft !== file.saved;

  const refreshTree = useCallback(() => {
    workspaces
      .tree(name)
      .then((t) => {
        setEntries(t.entries);
        setTruncated(t.truncated);
        setTreeError(null);
      })
      .catch(setTreeError);
  }, [name]);
  useEffect(refreshTree, [refreshTree]);

  // Warn before closing the tab with unsaved edits.
  useEffect(() => {
    if (!dirty) return;
    const onUnload = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [dirty]);

  const open = async (path: string) => {
    if (dirty && !window.confirm(`Discard unsaved changes to ${file!.path}?`)) return;
    setFileError(null);
    try {
      const f = await workspaces.read(name, path);
      setFile({ path: f.path, saved: f.content });
      setEditorValue(f.content);
      setDraft(f.content);
    } catch (err) {
      setFileError(err);
    }
  };

  const save = useCallback(async () => {
    if (!file) return;
    setSaving(true);
    setFileError(null);
    const content = draft;
    try {
      await workspaces.write(name, file.path, content);
      setFile({ path: file.path, saved: content });
      setGitTick((n) => n + 1);
      refreshTree();
    } catch (err) {
      setFileError(err);
    } finally {
      setSaving(false);
    }
  }, [draft, file, name, refreshTree]);

  // The agent's applied changes: refresh the tree and keep the open file in step.
  const onAgentApplied = useCallback(
    (changed: Map<string, string | null>) => {
      refreshTree();
      setGitTick((n) => n + 1);
      if (file && changed.has(file.path)) {
        const next = changed.get(file.path)!;
        if (next === null) {
          setFile(null);
          setEditorValue("");
          setDraft("");
        } else {
          setFile({ path: file.path, saved: next });
          setEditorValue(next);
          setDraft(next);
        }
      } else if (!file) {
        const first = [...changed].find(([, v]) => v !== null);
        if (first) {
          setFile({ path: first[0], saved: first[1]! });
          setEditorValue(first[1]!);
          setDraft(first[1]!);
        }
      }
    },
    [file, refreshTree],
  );

  const afterTreeChange = (removedOrMoved?: { from: string; to?: string }) => {
    if (removedOrMoved && file) {
      const { from, to } = removedOrMoved;
      if (file.path === from || file.path.startsWith(`${from}/`)) {
        if (to) setFile({ ...file, path: to + file.path.slice(from.length) });
        else {
          setFile(null);
          setEditorValue("");
          setDraft("");
        }
      }
    }
    refreshTree();
    setGitTick((n) => n + 1);
  };

  return (
    <div className="ws-body">
      <aside className="ws-tree" aria-label="Files">
        <FileTree
          name={name}
          entries={entries}
          truncated={truncated}
          activePath={file?.path ?? null}
          onOpen={(p) => void open(p)}
          onChanged={afterTreeChange}
          onCreatedFile={(p) => void open(p)}
        />
        <ErrorBanner error={treeError} onRetry={refreshTree} />
      </aside>

      <section className="ws-main">
        <div className="ws-file-bar">
          {file ? (
            <>
              <span className="mono ws-file-path">
                {file.path}
                {dirty && <span className="ws-dirty" title="Unsaved changes" aria-label="unsaved" />}
              </span>
              <span className="muted small mono">{formatInt(draft.split("\n").length)} lines</span>
              <button type="button" className="btn btn-small btn-primary" onClick={() => void save()} disabled={!dirty || saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              <span className="muted small">
                <kbd>Ctrl</kbd>+<kbd>S</kbd>
              </span>
            </>
          ) : (
            <span className="muted small">Pick a file on the left, or create one.</span>
          )}
        </div>
        <ErrorBanner error={fileError} />
        <div className="ws-editor" hidden={!file}>
          <CodeEditor path={file?.path ?? ""} value={editorValue} onChange={setDraft} onSave={() => void save()} />
        </div>
      </section>

      <aside className="ws-side">
        <div className="ws-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={side === "agent"} className={side === "agent" ? "active" : ""} onClick={() => setSide("agent")}>
            Agent
          </button>
          <button type="button" role="tab" aria-selected={side === "ai"} className={side === "ai" ? "active" : ""} onClick={() => setSide("ai")}>
            AI edit
          </button>
          <button type="button" role="tab" aria-selected={side === "git"} className={side === "git" ? "active" : ""} onClick={() => setSide("git")}>
            Git
          </button>
        </div>
        <div className="ws-side-pane" hidden={side !== "agent"}>
          <AgentPanel name={name} entries={entries} openFile={file ? { path: file.path, content: draft } : null} onApplied={onAgentApplied} />
        </div>
        {side === "agent" ? null : side === "ai" ? (
          <AiEditPanel
            file={file ? { path: file.path, content: draft } : null}
            onApply={(text) => {
              setEditorValue(text);
              setDraft(text);
            }}
          />
        ) : (
          <GitPanel
            name={name}
            tick={gitTick}
            onOpen={(p) => void open(p)}
            onCommitted={() => setGitTick((n) => n + 1)}
            onRemoteChanged={onGitChanged}
          />
        )}
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------------------- tree

interface TreeNode {
  name: string;
  path: string;
  type: "file" | "dir";
  size: number;
  children: TreeNode[];
}

function buildTree(entries: WorkspaceEntry[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", type: "dir", size: 0, children: [] };
  const dirs = new Map<string, TreeNode>([["", root]]);
  const ensureDir = (path: string): TreeNode => {
    const existing = dirs.get(path);
    if (existing) return existing;
    const idx = path.lastIndexOf("/");
    const parent = ensureDir(idx < 0 ? "" : path.slice(0, idx));
    const node: TreeNode = { name: path.slice(idx + 1), path, type: "dir", size: 0, children: [] };
    parent.children.push(node);
    dirs.set(path, node);
    return node;
  };
  for (const e of entries) {
    if (e.type === "dir") {
      ensureDir(e.path);
      continue;
    }
    const idx = e.path.lastIndexOf("/");
    ensureDir(idx < 0 ? "" : e.path.slice(0, idx)).children.push({
      name: e.path.slice(idx + 1),
      path: e.path,
      type: "file",
      size: e.size,
      children: [],
    });
  }
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1));
    nodes.forEach((n) => sort(n.children));
  };
  sort(root.children);
  return root.children;
}

type TreeEdit = { mode: "file" | "folder" | "rename"; base: string; value: string };

function FileTree({
  name,
  entries,
  truncated,
  activePath,
  onOpen,
  onChanged,
  onCreatedFile,
}: {
  name: string;
  entries: WorkspaceEntry[] | null;
  truncated: boolean;
  activePath: string | null;
  onOpen: (path: string) => void;
  onChanged: (change?: { from: string; to?: string }) => void;
  onCreatedFile: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selectedDir, setSelectedDir] = useState("");
  const [edit, setEdit] = useState<TreeEdit | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const tree = useMemo(() => buildTree(entries ?? []), [entries]);

  const toggle = (path: string) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const startCreate = (mode: "file" | "folder") => setEdit({ mode, base: "", value: selectedDir ? `${selectedDir}/` : "" });

  const commitEdit = async () => {
    if (!edit) return;
    const value = edit.value.trim().replace(/^\/+|\/+$/g, "");
    if (!value) return setEdit(null);
    setError(null);
    try {
      if (edit.mode === "folder") {
        await workspaces.mkdir(name, value);
        onChanged();
      } else if (edit.mode === "file") {
        await workspaces.write(name, value, "");
        onChanged();
        onCreatedFile(value);
      } else if (value !== edit.base) {
        await workspaces.move(name, edit.base, value);
        onChanged({ from: edit.base, to: value });
      }
      // Show where the new entry landed.
      const parts = value.split("/");
      setExpanded((s) => new Set([...s, ...parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"))]));
      setEdit(null);
    } catch (err) {
      setError(err);
    }
  };

  const remove = async (path: string) => {
    setConfirmDelete(null);
    setError(null);
    try {
      await workspaces.removeEntry(name, path);
      onChanged({ from: path });
    } catch (err) {
      setError(err);
    }
  };

  const renderNodes = (nodes: TreeNode[], depth: number) =>
    nodes.map((n) => (
      <li key={n.path}>
        <div
          className={`tree-row${n.path === activePath ? " active" : ""}${n.type === "dir" && n.path === selectedDir ? " selected" : ""}`}
          style={{ paddingLeft: 8 + depth * 12 }}
        >
          <button
            type="button"
            className="tree-name"
            onClick={() => {
              if (n.type === "dir") {
                toggle(n.path);
                setSelectedDir(n.path);
              } else {
                onOpen(n.path);
                setSelectedDir(n.path.includes("/") ? n.path.slice(0, n.path.lastIndexOf("/")) : "");
              }
            }}
            title={n.type === "file" ? `${n.path} · ${formatInt(n.size)} bytes` : n.path}
          >
            <span className="tree-icon" aria-hidden="true">
              {n.type === "dir" ? (expanded.has(n.path) ? "▾" : "▸") : ""}
            </span>
            {n.name}
          </button>
          <span className="tree-actions">
            <button type="button" onClick={() => setEdit({ mode: "rename", base: n.path, value: n.path })} aria-label={`Rename ${n.path}`}>
              rename
            </button>
            {confirmDelete === n.path ? (
              <button type="button" className="confirm" autoFocus onBlur={() => setConfirmDelete(null)} onClick={() => void remove(n.path)}>
                delete?
              </button>
            ) : (
              <button type="button" onClick={() => setConfirmDelete(n.path)} aria-label={`Delete ${n.path}`}>
                ×
              </button>
            )}
          </span>
        </div>
        {n.type === "dir" && expanded.has(n.path) && n.children.length > 0 && <ul>{renderNodes(n.children, depth + 1)}</ul>}
      </li>
    ));

  return (
    <div className="tree">
      <div className="tree-head">
        <span className="section-label">Files</span>
        <span>
          <button type="button" className="btn btn-small" onClick={() => startCreate("file")}>
            + File
          </button>
          <button type="button" className="btn btn-small" onClick={() => startCreate("folder")}>
            + Folder
          </button>
        </span>
      </div>
      {edit && (
        <form
          className="tree-edit"
          onSubmit={(e) => {
            e.preventDefault();
            void commitEdit();
          }}
        >
          <input
            className="mono"
            autoFocus
            value={edit.value}
            placeholder={edit.mode === "folder" ? "path/to/folder" : "path/to/file.ts"}
            aria-label={edit.mode === "rename" ? `New path for ${edit.base}` : `New ${edit.mode} path`}
            onChange={(e) => setEdit({ ...edit, value: e.target.value })}
            onKeyDown={(e) => e.key === "Escape" && setEdit(null)}
          />
          <button type="submit" className="btn btn-small btn-primary">
            {edit.mode === "rename" ? "Rename" : "Create"}
          </button>
        </form>
      )}
      <ErrorBanner error={error} />
      {entries === null ? (
        <div className="muted small pad">Loading…</div>
      ) : entries.length === 0 ? (
        <div className="muted small pad">Empty. Add a file to start.</div>
      ) : (
        <ul className="tree-list">{renderNodes(tree, 0)}</ul>
      )}
      {truncated && <div className="muted small pad">Showing the first {formatInt(entries?.length ?? 0)} entries.</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------- AI edit

const EDIT_SYSTEM_PROMPT =
  "You edit one file of a software project. Apply the requested change and reply with the COMPLETE updated file " +
  "in a single fenced code block. Keep everything that the change does not touch exactly as it was. No explanation.";

function AiEditPanel({ file, onApply }: { file: { path: string; content: string } | null; onApply: (text: string) => void }) {
  const [models, setModels] = useState<OpenAIModel[]>([]);
  const [model, setModel] = useStoredState<string>("forge.chat.model", "");
  const [instruction, setInstruction] = useState("");
  const [running, setRunning] = useState(false);
  const [received, setReceived] = useState(0);
  const [proposal, setProposal] = useState<{ path: string; before: string; after: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const abortRef = useRef<AbortController | null>(null);

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

  // A proposal for another file (or an older version of this one) is stale.
  useEffect(() => {
    if (proposal && file?.path !== proposal.path) setProposal(null);
  }, [file?.path, proposal]);

  const run = async () => {
    if (!file || !instruction.trim() || !model) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    setReceived(0);
    setError(null);
    setProposal(null);
    const before = file.content;
    try {
      const result = await streamChat({
        model,
        signal: controller.signal,
        messages: [
          { role: "system", content: EDIT_SYSTEM_PROMPT },
          { role: "user", content: `File: ${file.path}\n\n\`\`\`\n${before}\n\`\`\`\n\nChange: ${instruction.trim()}` },
        ],
        onDelta: (d) => setReceived((n) => n + d.length),
      });
      setProposal({ path: file.path, before, after: extractCode(result.content) });
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) setError(err);
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  };

  if (!file) return <div className="muted small pad">Open a file, then describe the change you want.</div>;

  const lines = proposal ? diffLines(proposal.before, proposal.after) : [];
  const stats = diffStats(lines);
  const stale = proposal !== null && proposal.before !== file.content;

  return (
    <div className="ai-edit">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <textarea
          rows={4}
          value={instruction}
          placeholder={`What should change in ${file.path.split("/").pop()}?  e.g. "add input validation and a docstring"`}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void run();
            }
          }}
        />
        <div className="ai-edit-bar">
          <select value={model} onChange={(e) => setModel(e.target.value)} disabled={running} aria-label="Model">
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}
              </option>
            ))}
          </select>
          {running ? (
            <button type="button" className="btn btn-small" onClick={() => abortRef.current?.abort()}>
              Stop · {formatInt(received)} chars
            </button>
          ) : (
            <button type="submit" className="btn btn-small btn-primary" disabled={!instruction.trim() || !model}>
              Propose edit
            </button>
          )}
        </div>
        {file.content.length > AI_FILE_WARN_CHARS && (
          <p className="muted small">This file is long for an 8k-token model; the rewrite may get cut off. Review the diff closely.</p>
        )}
      </form>
      <ErrorBanner error={error} />
      {proposal && (
        <div className="proposal">
          <div className="proposal-head">
            <span className="mono small">
              <span className="diff-add-text">+{stats.added}</span> <span className="diff-del-text">−{stats.removed}</span>
            </span>
            <span>
              <button
                type="button"
                className="btn btn-small btn-primary"
                disabled={stats.added + stats.removed === 0}
                onClick={() => {
                  onApply(proposal.after);
                  setProposal(null);
                }}
              >
                Apply
              </button>
              <button type="button" className="btn btn-small" onClick={() => setProposal(null)}>
                Reject
              </button>
            </span>
          </div>
          {stale && <p className="muted small">The file changed since this was proposed; applying replaces your newer edits.</p>}
          {stats.added + stats.removed === 0 ? (
            <p className="muted small pad">The model returned the file unchanged.</p>
          ) : (
            <DiffView rows={foldUnchanged(lines)} />
          )}
          <p className="muted small">Apply puts the change in the editor; nothing is written until you save.</p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- git

function GitPanel({
  name,
  tick,
  onOpen,
  onCommitted,
  onRemoteChanged,
}: {
  name: string;
  tick: number;
  onOpen: (path: string) => void;
  onCommitted: () => void;
  onRemoteChanged: () => void;
}) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [diff, setDiff] = useState<{ diff: string; truncated: boolean } | null>(null);
  const [message, setMessage] = useState("");
  const [authorName, setAuthorName] = useStoredState<string>("forge.git.name", "");
  const [authorEmail, setAuthorEmail] = useStoredState<string>("forge.git.email", "");
  const [remote, setRemote] = useState("");
  // Never stored: the token lives in this input until the page is left.
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(() => {
    workspaces
      .gitStatus(name)
      .then((s) => {
        setStatus(s);
        setRemote((r) => r || s.remote || "");
        setError(null);
      })
      .catch(setError);
  }, [name]);
  useEffect(() => {
    refresh();
    setDiff(null);
  }, [refresh, tick]);

  const act = async (label: string, fn: () => Promise<string>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      setNotice(await fn());
      refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };

  const commit = () =>
    act("commit", async () => {
      const r = await workspaces.commit(name, message.trim(), authorName.trim(), authorEmail.trim());
      setMessage("");
      setDiff(null);
      onCommitted();
      return `Committed ${r.sha.slice(0, 7)} · ${r.summary}`;
    });

  const push = () =>
    act("push", async () => {
      if (remote.trim() && remote.trim() !== status?.remote) {
        await workspaces.setRemote(name, remote.trim());
        onRemoteChanged();
      }
      const r = await workspaces.push(name, token.trim() || null);
      return `Pushed ${r.branch} to origin.`;
    });

  if (!status) return <div className="pad">{error ? <ErrorBanner error={error} onRetry={refresh} /> : <span className="muted small">Loading…</span>}</div>;

  const canCommit = status.changes.length > 0 && message.trim() && authorName.trim() && authorEmail.includes("@");

  return (
    <div className="git">
      <dl className="git-meta mono small">
        <dt>branch</dt>
        <dd>{status.branch ?? "—"}</dd>
        <dt>head</dt>
        <dd>{status.head ?? "no commits"}</dd>
      </dl>

      <div className="git-section">
        <div className="git-section-head">
          <span className="section-label">Changes ({status.changes.length})</span>
          <span>
            <button type="button" className="btn btn-small" onClick={refresh}>
              Refresh
            </button>
            <button
              type="button"
              className="btn btn-small"
              disabled={status.changes.length === 0}
              onClick={() => (diff ? setDiff(null) : void workspaces.gitDiff(name).then(setDiff).catch(setError))}
            >
              {diff ? "Hide diff" : "Diff"}
            </button>
          </span>
        </div>
        {status.changes.length === 0 ? (
          <p className="muted small">Working tree clean.</p>
        ) : (
          <ul className="git-changes">
            {status.changes.map((c) => (
              <li key={c.path}>
                <span className={`git-code git-${c.status === "??" ? "new" : c.status.includes("D") ? "del" : "mod"}`}>
                  {c.status === "??" ? "U" : c.status}
                </span>
                {c.status.includes("D") ? (
                  <span className="mono">{c.path}</span>
                ) : (
                  <button type="button" className="link-button mono" onClick={() => onOpen(c.path)}>
                    {c.path}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {diff && <UnifiedDiff text={diff.diff} truncated={diff.truncated} />}
      </div>

      <form
        className="git-section"
        onSubmit={(e) => {
          e.preventDefault();
          if (canCommit) void commit();
        }}
      >
        <span className="section-label">Commit all changes</span>
        <textarea rows={2} placeholder="Commit message" value={message} onChange={(e) => setMessage(e.target.value)} />
        <div className="git-row">
          <input placeholder="Author name" value={authorName} onChange={(e) => setAuthorName(e.target.value)} aria-label="Author name" />
          <input placeholder="email" type="email" value={authorEmail} onChange={(e) => setAuthorEmail(e.target.value)} aria-label="Author email" />
        </div>
        <button type="submit" className="btn btn-small btn-primary" disabled={!canCommit || busy !== null}>
          {busy === "commit" ? "Committing…" : "Commit"}
        </button>
      </form>

      <form
        className="git-section"
        onSubmit={(e) => {
          e.preventDefault();
          void push();
        }}
      >
        <span className="section-label">Push</span>
        <input
          className="mono"
          type="url"
          placeholder="https://github.com/owner/repo.git"
          value={remote}
          onChange={(e) => setRemote(e.target.value)}
          aria-label="Remote URL"
        />
        <input
          type="password"
          autoComplete="off"
          placeholder="Access token (fine-grained, contents: write)"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-label="Access token"
        />
        <button type="submit" className="btn btn-small" disabled={!remote.trim() || !status.head || busy !== null}>
          {busy === "push" ? "Pushing…" : `Push ${status.branch ?? ""}`}
        </button>
        <p className="muted small">The token is sent with this push only and is never saved on the server or in the browser.</p>
      </form>

      {notice && <div className="banner banner-ok">{notice}</div>}
      <ErrorBanner error={error} />
    </div>
  );
}

function UnifiedDiff({ text, truncated }: { text: string; truncated: boolean }) {
  if (!text.trim()) return <p className="muted small">No textual changes.</p>;
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => {
        const kind = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")
          ? "meta"
          : line.startsWith("@@")
            ? "hunk"
            : line.startsWith("+")
              ? "add"
              : line.startsWith("-")
                ? "del"
                : "same";
        return (
          <div key={i} className={`diff-${kind}`}>
            {line || " "}
          </div>
        );
      })}
      {truncated && <div className="diff-gap">diff truncated</div>}
    </pre>
  );
}
