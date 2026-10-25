import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { parseChatChunk, streamChat, streamRepoAnswer } from "./stream";

function sseResponse(chunks: string[], headers: Record<string, string> = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream", ...headers } });
}

afterEach(() => vi.unstubAllGlobals());

describe("parseChatChunk", () => {
  it("reads content deltas, finish reasons and the usage chunk", () => {
    expect(parseChatChunk('{"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}')).toEqual({
      done: false,
      delta: "hi",
      finishReason: null,
      usage: null,
    });
    expect(parseChatChunk('{"choices":[{"delta":{},"finish_reason":"stop"}]}').finishReason).toBe("stop");
    expect(parseChatChunk('{"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":9}}').usage).toEqual({
      prompt_tokens: 3,
      completion_tokens: 9,
    });
    expect(parseChatChunk("[DONE]").done).toBe(true);
  });

  it("turns an in-stream error object into ApiError", () => {
    expect(() => parseChatChunk('{"error":{"message":"backend went away","type":"backend_unavailable"}}')).toThrow(ApiError);
  });

  it("rejects malformed JSON", () => {
    expect(() => parseChatChunk("{not json")).toThrow(/malformed/);
  });
});

describe("streamChat", () => {
  it("streams deltas split mid-line and reports usage and queue time", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse(
        [
          'data: {"model":"forge-chat","choices":[{"delta":{"role":"assistant","content":"Hel',
          'lo"}}]}\n\ndata: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n',
          '\ndata: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\ndata: [DO',
          "NE]\n\n",
        ],
        { "x-forge-queue-ms": "12" },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const deltas: string[] = [];
    const result = await streamChat({
      model: "forge-chat",
      messages: [{ role: "user", content: "hi" }],
      signal: new AbortController().signal,
      onDelta: (d) => deltas.push(d),
    });

    expect(deltas).toEqual(["Hello", " world"]);
    expect(result.content).toBe("Hello world");
    expect(result.finishReason).toBe("stop");
    expect(result.usage).toEqual({ prompt_tokens: 4, completion_tokens: 2 });
    expect(result.stats.queueMs).toBe(12);
    expect(result.stats.completionTokens).toBe(2);
    expect(result.stats.ttftMs).not.toBeNull();

    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    const sent = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(sent.stream).toBe(true);
    expect(sent.stream_options).toEqual({ include_usage: true });
  });

  it("fails when the stream ends without [DONE] or a finish reason", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse(['data: {"choices":[{"delta":{"content":"par"}}]}\n\n'])));
    await expect(
      streamChat({ model: "forge-chat", messages: [], signal: new AbortController().signal, onDelta: () => undefined }),
    ).rejects.toMatchObject({ type: "stream_interrupted" });
  });
});

describe("streamRepoAnswer", () => {
  it("collects sources, tokens and usage from named events", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          'event: sources\ndata: [{"n":1,"path":"src/app.py","start_line":1,"end_line":60,"score":0.9}]\n\nevent: tok',
          'en\ndata: {"t":"It starts in "}\n\nevent: token\ndata: {"t":"app.py [1]."}\n\n',
          'event: done\ndata: {"usage":{"prompt_tokens":900,"completion_tokens":7}}\n\n',
        ]),
      ),
    );
    const sourcesSeen: number[] = [];
    const result = await streamRepoAnswer({
      repoId: 3,
      question: "Where does it start?",
      topK: 8,
      signal: new AbortController().signal,
      onSources: (s) => sourcesSeen.push(s.length),
      onToken: () => undefined,
    });
    expect(sourcesSeen).toEqual([1]);
    expect(result.answer).toBe("It starts in app.py [1].");
    expect(result.sources[0]!.path).toBe("src/app.py");
    expect(result.usage?.completion_tokens).toBe(7);
  });

  it("surfaces an error event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse(['event: error\ndata: {"message":"repo is not indexed"}\n\n'])));
    await expect(
      streamRepoAnswer({
        repoId: 3,
        question: "q",
        topK: 8,
        signal: new AbortController().signal,
        onSources: () => undefined,
        onToken: () => undefined,
      }),
    ).rejects.toMatchObject({ type: "rag_error", message: "repo is not indexed" });
  });
});
