import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { describeError } from "../api/client";
import { conversations, listOpenAIModels } from "../api/endpoints";
import { streamChat } from "../api/stream";
import type { ConversationSummary, OpenAIModel } from "../api/types";
import { Markdown } from "../components/Markdown";
import { EmptyState, ErrorBanner, useDocumentTitle } from "../components/ui";
import { uuid } from "../lib/browser";
import { toModelMessages, toStoredMessages, toTurns, type Turn } from "../lib/conversation";
import { formatDuration, formatInt, formatMs, formatRate, formatRelative } from "../lib/format";
import { PRESETS, presetForSystemPrompt } from "../lib/presets";
import { useStoredState } from "../lib/useStoredState";

const TITLE_MAX = 60;

function titleFrom(text: string): string {
  const line = text.trim().split("\n")[0] ?? "";
  return line.length > TITLE_MAX ? `${line.slice(0, TITLE_MAX - 1)}…` : line || "Untitled";
}

export function ChatPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  useDocumentTitle("Chat");

  const [models, setModels] = useState<OpenAIModel[] | null>(null);
  const [modelsError, setModelsError] = useState<unknown>(null);
  const [model, setModel] = useStoredState<string>("forge.chat.model", "");
  const [presetId, setPresetId] = useStoredState<string>("forge.chat.preset", "free");
  const preset = PRESETS.find((p) => p.id === presetId) ?? PRESETS[0]!;

  const [history, setHistory] = useState<ConversationSummary[] | null>(null);
  const [historyError, setHistoryError] = useState<unknown>(null);
  // Narrow screens show the history as a drawer instead of a column.
  const [historyOpen, setHistoryOpen] = useState(false);
  const [turns, setTurnsState] = useState<Turn[]>([]);
  // Mirror of `turns` so async stream callbacks can read the latest list synchronously.
  const turnsRef = useRef<Turn[]>([]);
  const setTurns = useCallback((next: Turn[] | ((ts: Turn[]) => Turn[])) => {
    turnsRef.current = typeof next === "function" ? next(turnsRef.current) : next;
    setTurnsState(turnsRef.current);
  }, []);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [waitStart, setWaitStart] = useState<number | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  // The conversation currently held in state; lets a freshly created chat change
  // the URL without the loader refetching (and 404ing) a not-yet-saved id.
  const activeIdRef = useRef<string | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const chatModels = useMemo(() => (models ?? []).filter((m) => !m.role || m.role === "chat"), [models]);

  useEffect(() => {
    const controller = new AbortController();
    listOpenAIModels(controller.signal)
      .then((list) => setModels(list))
      .catch((err: unknown) => {
        if (!(err instanceof DOMException)) setModelsError(err);
      });
    return () => controller.abort();
  }, []);

  // Fall back to the first chat model when the remembered one was removed from models.yaml.
  useEffect(() => {
    if (chatModels.length > 0 && !chatModels.some((m) => m.id === model)) setModel(chatModels[0]!.id);
  }, [chatModels, model, setModel]);

  const refreshHistory = useCallback(() => {
    conversations
      .list()
      .then((list) => {
        setHistory([...list].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)));
        setHistoryError(null);
      })
      .catch((err: unknown) => setHistoryError(err));
  }, []);

  useEffect(refreshHistory, [refreshHistory]);

  // Load the conversation named in the URL.
  useEffect(() => {
    if (!conversationId) {
      if (activeIdRef.current !== null) {
        abortRef.current?.abort();
        activeIdRef.current = null;
        setTurns([]);
      }
      return;
    }
    if (conversationId === activeIdRef.current) return;

    abortRef.current?.abort();
    activeIdRef.current = conversationId;
    setTurns([]);
    setLoadError(null);
    let cancelled = false;
    conversations
      .get(conversationId)
      .then((conv) => {
        if (cancelled) return;
        setTurns(toTurns(conv.messages ?? [], uuid));
        const system = conv.messages?.find((m) => m.role === "system")?.content;
        setPresetId(presetForSystemPrompt(system).id);
        if (conv.model) setModel(conv.model);
        stickToBottom.current = true;
      })
      .catch((err: unknown) => !cancelled && setLoadError(err));
    return () => {
      cancelled = true;
    };
  }, [conversationId, setModel, setPresetId, setTurns]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // Picking a conversation (or starting a new one) closes the drawer.
  useEffect(() => setHistoryOpen(false), [conversationId]);

  useEffect(() => {
    const el = threadRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

  // Ticks the "waiting for first token" counter.
  const [, forceTick] = useState(0);
  useEffect(() => {
    if (waitStart === null) return;
    const id = window.setInterval(() => forceTick((n) => n + 1), 500);
    return () => window.clearInterval(id);
  }, [waitStart]);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  useEffect(() => {
    if (!streaming) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") stop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [streaming, stop]);

  const save = useCallback(
    async (id: string, finalTurns: Turn[]) => {
      const firstUser = finalTurns.find((t) => t.role === "user");
      try {
        await conversations.save(id, {
          title: titleFrom(firstUser?.content ?? ""),
          model,
          messages: toStoredMessages(preset.system, finalTurns),
        });
        setSaveError(null);
        refreshHistory();
      } catch (err) {
        setSaveError(err);
      }
    },
    [model, preset, refreshHistory],
  );

  const send = useCallback(
    async (text: string, base: Turn[]) => {
      if (!text.trim() || streaming || !model) return;

      let id = activeIdRef.current;
      if (!id) {
        id = uuid();
        activeIdRef.current = id;
        navigate(`/chat/${id}`);
      }

      const userTurn: Turn = { key: uuid(), role: "user", content: text };
      const assistantKey = uuid();
      const prior = [...base, userTurn];
      setTurns([...prior, { key: assistantKey, role: "assistant", content: "", pending: true }]);
      setInput("");
      setStreaming(true);
      setWaitStart(Date.now());
      stickToBottom.current = true;

      const controller = new AbortController();
      abortRef.current = controller;

      // Batch deltas: re-rendering markdown + highlighting per token is wasteful.
      let buffered = "";
      let flushTimer: number | undefined;
      const flush = () => {
        flushTimer = undefined;
        if (!buffered) return;
        const chunk = buffered;
        buffered = "";
        setTurns((ts) => ts.map((t) => (t.key === assistantKey ? { ...t, content: t.content + chunk } : t)));
      };

      const finish = (patch: Partial<Turn>) => {
        window.clearTimeout(flushTimer);
        flush();
        setTurns((ts) => ts.map((t) => (t.key === assistantKey ? { ...t, ...patch, pending: false } : t)));
        return turnsRef.current;
      };

      const payload = toModelMessages(preset.system, prior);
      let content = "";
      try {
        const result = await streamChat({
          model,
          messages: payload,
          signal: controller.signal,
          onDelta: (delta) => {
            if (!content) setWaitStart(null);
            content += delta;
            buffered += delta;
            if (flushTimer === undefined) flushTimer = window.setTimeout(flush, 40);
          },
        });
        const finalTurns = finish({ stats: result.stats });
        if (activeIdRef.current === id) await save(id, finalTurns);
      } catch (err) {
        const aborted = err instanceof DOMException && err.name === "AbortError";
        const finalTurns = finish(aborted ? { stopped: true } : { error: describeError(err) });
        // A stopped answer is still worth keeping, and so is a failed one (with its error, so the
        // reopened question doesn't look unanswered); a request stopped before any text is not.
        if (activeIdRef.current === id && (aborted ? content : true)) await save(id, finalTurns);
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
        setStreaming(false);
        setWaitStart(null);
        inputRef.current?.focus();
      }
    },
    [model, navigate, preset, save, setTurns, streaming],
  );

  const retry = (assistantKey: string) => {
    const idx = turns.findIndex((t) => t.key === assistantKey);
    const user = turns[idx - 1];
    if (idx < 1 || !user || user.role !== "user") return;
    void send(user.content, turns.slice(0, idx - 1));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(input, turns);
    }
  };

  const newChat = () => {
    abortRef.current?.abort();
    setHistoryOpen(false);
    navigate("/chat");
    inputRef.current?.focus();
  };

  const removeConversation = async (id: string) => {
    try {
      await conversations.remove(id);
      if (id === activeIdRef.current) newChat();
      refreshHistory();
    } catch (err) {
      setHistoryError(err);
    }
  };

  const waitingFor = waitStart !== null ? (Date.now() - waitStart) / 1000 : 0;
  const noModels = models !== null && chatModels.length === 0;

  return (
    <div className={`chat${historyOpen ? " history-open" : ""}`}>
      {historyOpen && <div className="history-backdrop" onClick={() => setHistoryOpen(false)} aria-hidden="true" />}
      <aside id="chat-history" className="chat-history" aria-label="Conversations">
        <div className="chat-history-head">
          <span className="section-label">History</span>
          <button type="button" className="btn btn-small" onClick={newChat}>
            New chat
          </button>
        </div>
        <ErrorBanner error={historyError} onRetry={refreshHistory} />
        <HistoryList history={history} activeId={conversationId ?? null} onDelete={(id) => void removeConversation(id)} />
      </aside>

      <section className="chat-main">
        <div className="toolbar">
          <button
            type="button"
            className="btn btn-small history-toggle"
            aria-controls="chat-history"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((open) => !open)}
          >
            History
          </button>
          <label className="inline-field">
            <span>Model</span>
            <select value={model} onChange={(e) => setModel(e.target.value)} disabled={streaming || chatModels.length === 0}>
              {chatModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            <span>Preset</span>
            <select value={preset.id} onChange={(e) => setPresetId(e.target.value)} disabled={streaming}>
              {PRESETS.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <span className="toolbar-hint">{preset.hint}</span>
        </div>

        <div
          className="thread"
          ref={threadRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
          }}
        >
          <ErrorBanner error={modelsError} />
          <ErrorBanner error={loadError} />
          {noModels && (
            <EmptyState title="No chat model is configured">
              Add a model with <code>role: chat</code> to <code>gitops/models.yaml</code> and let Argo CD sync it.
            </EmptyState>
          )}
          {!noModels && turns.length === 0 && !loadError && (
            <div className="thread-empty">
              <div className="section-label">Start with a preset</div>
              <ul className="preset-list">
                {PRESETS.filter((p) => p.system).map((p) => (
                  <li key={p.id}>
                    <button type="button" className={`preset ${p.id === preset.id ? "active" : ""}`} onClick={() => setPresetId(p.id)}>
                      <span className="preset-name">{p.label}</span>
                      <span className="preset-hint">{p.hint}</span>
                    </button>
                  </li>
                ))}
              </ul>
              <p className="muted small">
                Paste code into the box below. The first answer after an idle period can take a minute while the GPU
                backend scales up from zero; see <Link to="/system">System</Link>.
              </p>
            </div>
          )}
          {turns.map((t) => (
            <article key={t.key} className={`turn turn-${t.role}`}>
              <div className="turn-role">{t.role === "user" ? "you" : model || "assistant"}</div>
              <div className="turn-body">
                {t.role === "user" ? (
                  <div className="turn-user-text">{t.content}</div>
                ) : t.content ? (
                  <Markdown content={t.content} />
                ) : t.pending ? (
                  <div className="waiting">
                    <span className="caret" aria-hidden="true" />
                    Waiting for a GPU slot · <span className="mono">{formatDuration(waitingFor)}</span>
                    {waitingFor > 5 && <span className="muted"> · the backend may be waking up or loading the model</span>}
                  </div>
                ) : null}
                {t.pending && t.content && <span className="caret" aria-hidden="true" />}
                {t.role === "assistant" && !t.pending && <TurnFooter turn={t} onRetry={() => retry(t.key)} canRetry={!streaming} />}
              </div>
            </article>
          ))}
          <ErrorBanner error={saveError} />
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send(input, turns);
          }}
        >
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={noModels ? "No chat model available" : "Ask about code, or paste a snippet"}
            rows={Math.min(12, Math.max(3, input.split("\n").length))}
            disabled={noModels}
            autoFocus
          />
          <div className="composer-bar">
            <span className="muted small">
              <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline · <kbd>Esc</kbd> stop
            </span>
            {streaming ? (
              <button type="button" className="btn" onClick={stop}>
                Stop
              </button>
            ) : (
              <button type="submit" className="btn btn-primary" disabled={!input.trim() || !model}>
                Send
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}

function TurnFooter({ turn, onRetry, canRetry }: { turn: Turn; onRetry: () => void; canRetry: boolean }) {
  const s = turn.stats;
  return (
    <div className="turn-foot">
      {turn.error && (
        <span className="turn-error">
          {turn.error}{" "}
          <button type="button" className="link-button" onClick={onRetry} disabled={!canRetry}>
            Retry
          </button>
        </span>
      )}
      {turn.stopped && <span className="muted">stopped</span>}
      {s && (
        <span className="mono muted turn-stats">
          {formatInt(s.completionTokens)} tok · {formatRate(s.tokensPerSecond)} · ttft {formatMs(s.ttftMs)}
          {s.queueMs !== null && s.queueMs > 0 && <> · queue {formatMs(s.queueMs)}</>}
        </span>
      )}
    </div>
  );
}

function HistoryList({
  history,
  activeId,
  onDelete,
}: {
  history: ConversationSummary[] | null;
  activeId: string | null;
  onDelete: (id: string) => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  if (history === null) return <div className="muted small pad">Loading…</div>;
  if (history.length === 0) return <div className="muted small pad">Saved conversations appear here after the first answer.</div>;
  return (
    <ul className="history-list">
      {history.map((c) => (
        <li key={c.id} className={c.id === activeId ? "active" : ""}>
          <Link to={`/chat/${c.id}`} className="history-link" title={c.title}>
            <span className="history-title">{c.title || "Untitled"}</span>
            <span className="history-time mono">{formatRelative(c.updated_at)}</span>
          </Link>
          {confirming === c.id ? (
            <button type="button" className="history-del confirm" onClick={() => onDelete(c.id)} onBlur={() => setConfirming(null)} autoFocus>
              delete?
            </button>
          ) : (
            <button type="button" className="history-del" onClick={() => setConfirming(c.id)} aria-label={`Delete ${c.title}`}>
              ×
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

