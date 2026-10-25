/** Streaming calls: OpenAI chat chunks (/v1/chat/completions) and the RAG event stream (/api/repos/{id}/ask). */
import { ApiError, request } from "./client";
import { readSSE } from "./sse";
import type { ChatMessage, RagSource, TokenUsage } from "./types";

export interface ChatChunk {
  done: boolean;
  delta: string;
  finishReason: string | null;
  usage: TokenUsage | null;
}

/**
 * Interprets one `data:` payload of an OpenAI chat stream. Throws ApiError when
 * the server reports an error inside the stream (after the 200 headers were sent).
 */
export function parseChatChunk(data: string): ChatChunk {
  if (data.trim() === "[DONE]") return { done: true, delta: "", finishReason: null, usage: null };

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(data) as Record<string, unknown>;
  } catch {
    throw new ApiError({ status: 502, type: "bad_stream", message: "The server sent a malformed stream chunk." });
  }

  const error = payload.error as Record<string, unknown> | undefined;
  if (error && typeof error === "object") {
    throw new ApiError({
      status: 502,
      type: typeof error.type === "string" ? error.type : "server_error",
      message: typeof error.message === "string" ? error.message : "The model stream failed.",
      code: typeof error.code === "string" ? error.code : null,
    });
  }

  const choices = Array.isArray(payload.choices) ? (payload.choices as Record<string, unknown>[]) : [];
  const first = choices[0];
  const delta = first && typeof first.delta === "object" && first.delta !== null ? (first.delta as Record<string, unknown>) : {};
  const usage = payload.usage && typeof payload.usage === "object" ? (payload.usage as TokenUsage) : null;

  return {
    done: false,
    delta: typeof delta.content === "string" ? delta.content : "",
    finishReason: first && typeof first.finish_reason === "string" ? first.finish_reason : null,
    usage,
  };
}

export interface StreamStats {
  /** Time from sending the request to the first text, including GPU queue and cold start. */
  ttftMs: number | null;
  durationMs: number;
  /** Gateway-reported wait for a GPU slot (x-forge-queue-ms). */
  queueMs: number | null;
  completionTokens: number | null;
  /** Generation speed after the first token, so queueing does not distort it. */
  tokensPerSecond: number | null;
}

export interface ChatResult {
  content: string;
  finishReason: string | null;
  usage: TokenUsage | null;
  stats: StreamStats;
}

function buildStats(start: number, firstAt: number | null, end: number, completionTokens: number | null, queueHeader: string | null): StreamStats {
  const genMs = firstAt !== null ? end - firstAt : 0;
  const queue = queueHeader !== null && queueHeader !== "" ? Number(queueHeader) : NaN;
  return {
    ttftMs: firstAt !== null ? Math.round(firstAt - start) : null,
    durationMs: Math.round(end - start),
    queueMs: Number.isFinite(queue) ? queue : null,
    completionTokens,
    tokensPerSecond: completionTokens && genMs > 0 ? (completionTokens / genMs) * 1000 : null,
  };
}

export async function streamChat(opts: {
  model: string;
  messages: ChatMessage[];
  signal: AbortSignal;
  onDelta: (text: string) => void;
}): Promise<ChatResult> {
  const start = performance.now();
  const response = await request("/v1/chat/completions", {
    method: "POST",
    body: { model: opts.model, messages: opts.messages, stream: true, stream_options: { include_usage: true } },
    signal: opts.signal,
    headers: { Accept: "text/event-stream" },
  });
  if (!response.body) throw new ApiError({ status: 502, type: "bad_stream", message: "The response had no body." });

  let content = "";
  let firstAt: number | null = null;
  let finishReason: string | null = null;
  let usage: TokenUsage | null = null;
  let sawDone = false;

  for await (const event of readSSE(response.body)) {
    const chunk = parseChatChunk(event.data);
    if (chunk.done) {
      sawDone = true;
      break;
    }
    if (chunk.usage) usage = chunk.usage;
    if (chunk.finishReason) finishReason = chunk.finishReason;
    if (chunk.delta) {
      if (firstAt === null) firstAt = performance.now();
      content += chunk.delta;
      opts.onDelta(chunk.delta);
    }
  }

  if (!sawDone && finishReason === null) {
    throw new ApiError({ status: 502, type: "stream_interrupted", message: "The connection closed before the answer finished." });
  }

  const stats = buildStats(start, firstAt, performance.now(), usage?.completion_tokens ?? null, response.headers.get("x-forge-queue-ms"));
  return { content, finishReason, usage, stats };
}

export interface RagResult {
  answer: string;
  sources: RagSource[];
  usage: TokenUsage | null;
  stats: StreamStats;
}

export async function streamRepoAnswer(opts: {
  repoId: number;
  question: string;
  topK: number;
  signal: AbortSignal;
  onSources: (sources: RagSource[]) => void;
  onToken: (text: string) => void;
}): Promise<RagResult> {
  const start = performance.now();
  const response = await request(`/api/repos/${opts.repoId}/ask`, {
    method: "POST",
    body: { question: opts.question, top_k: opts.topK },
    signal: opts.signal,
    headers: { Accept: "text/event-stream" },
  });
  if (!response.body) throw new ApiError({ status: 502, type: "bad_stream", message: "The response had no body." });

  let answer = "";
  let sources: RagSource[] = [];
  let usage: TokenUsage | null = null;
  let firstAt: number | null = null;
  let finished = false;

  for await (const event of readSSE(response.body)) {
    let data: unknown;
    try {
      data = JSON.parse(event.data);
    } catch {
      throw new ApiError({ status: 502, type: "bad_stream", message: `Malformed "${event.event}" event from the server.` });
    }
    const obj = (data ?? {}) as Record<string, unknown>;

    if (event.event === "sources") {
      sources = Array.isArray(data) ? (data as RagSource[]) : [];
      opts.onSources(sources);
    } else if (event.event === "token") {
      const t = typeof obj.t === "string" ? obj.t : "";
      if (t) {
        if (firstAt === null) firstAt = performance.now();
        answer += t;
        opts.onToken(t);
      }
    } else if (event.event === "done") {
      usage = obj.usage && typeof obj.usage === "object" ? (obj.usage as TokenUsage) : null;
      finished = true;
      break;
    } else if (event.event === "error") {
      throw new ApiError({
        status: 502,
        type: "rag_error",
        message: typeof obj.message === "string" ? obj.message : "Answering failed.",
      });
    }
  }

  if (!finished) {
    throw new ApiError({ status: 502, type: "stream_interrupted", message: "The connection closed before the answer finished." });
  }
  const stats = buildStats(start, firstAt, performance.now(), usage?.completion_tokens ?? null, response.headers.get("x-forge-queue-ms"));
  return { answer, sources, usage, stats };
}
