/**
 * Converting between the chat page's turns and what is saved in /api/conversations
 * or sent to the model. Saved messages keep per-answer details (stats, the
 * "stopped" marker, errors) so a reopened conversation looks like it did live;
 * the model only ever gets plain {role, content} from finished exchanges.
 */
import type { StreamStats } from "../api/stream";
import type { ChatMessage, StoredMessage } from "../api/types";

export interface Turn {
  key: string;
  role: "user" | "assistant";
  content: string;
  stats?: StreamStats;
  /** Set while this assistant turn is being generated. */
  pending?: boolean;
  stopped?: boolean;
  error?: string;
}

function readStats(value: unknown): StreamStats | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const o = value as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  return {
    ttftMs: num(o.ttftMs),
    durationMs: num(o.durationMs) ?? 0,
    queueMs: num(o.queueMs),
    completionTokens: num(o.completionTokens),
    tokensPerSecond: num(o.tokensPerSecond),
  };
}

export function toTurns(messages: StoredMessage[], newKey: () => string): Turn[] {
  const turns: Turn[] = [];
  for (const m of messages) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    const turn: Turn = { key: newKey(), role: m.role, content: typeof m.content === "string" ? m.content : "" };
    const stats = readStats(m.stats);
    if (stats) turn.stats = stats;
    if (m.stopped === true) turn.stopped = true;
    if (typeof m.error === "string" && m.error) turn.error = m.error;
    turns.push(turn);
  }
  return turns;
}

/** Everything the user saw, including failed answers (with their error), so nothing looks unanswered. */
export function toStoredMessages(system: string | null, turns: Turn[]): StoredMessage[] {
  const out: StoredMessage[] = system ? [{ role: "system", content: system }] : [];
  for (const t of turns) {
    if (t.pending) continue;
    const m: StoredMessage = { role: t.role, content: t.content };
    if (t.stats) m.stats = { ...t.stats };
    if (t.stopped) m.stopped = true;
    if (t.error) m.error = t.error;
    out.push(m);
  }
  return out;
}

/**
 * The history sent with the next question. An exchange whose answer failed
 * before any text arrived is left out entirely: otherwise the model would see
 * two user messages in a row and answer the old question too.
 */
export function toModelMessages(system: string | null, turns: Turn[]): ChatMessage[] {
  const out: ChatMessage[] = system ? [{ role: "system", content: system }] : [];
  for (const t of turns) {
    if (t.pending) continue;
    if (t.role === "assistant" && t.error && !t.content) {
      if (out[out.length - 1]?.role === "user") out.pop();
      continue;
    }
    out.push({ role: t.role, content: t.content });
  }
  return out;
}
