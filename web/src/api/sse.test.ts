import { describe, expect, it } from "vitest";
import { SSEParser, readSSE, type SSEEvent } from "./sse";

function parseAll(chunks: string[]): SSEEvent[] {
  const parser = new SSEParser();
  const events = chunks.flatMap((c) => parser.feed(c));
  return [...events, ...parser.end()];
}

function bytesStream(parts: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const p of parts) controller.enqueue(p);
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<SSEEvent[]> {
  const out: SSEEvent[] = [];
  for await (const e of readSSE(stream)) out.push(e);
  return out;
}

const openAiStream =
  'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n' +
  'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n' +
  'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n' +
  "data: [DONE]\n\n";

describe("SSEParser", () => {
  it("parses a complete OpenAI chat stream in one chunk", () => {
    const events = parseAll([openAiStream]);
    expect(events.map((e) => e.data)).toEqual([
      '{"choices":[{"delta":{"content":"Hel"}}]}',
      '{"choices":[{"delta":{"content":"lo"}}]}',
      '{"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
      "[DONE]",
    ]);
    expect(events.every((e) => e.event === "message")).toBe(true);
  });

  it("gives identical results for every possible split point", () => {
    const expected = parseAll([openAiStream]);
    for (let i = 1; i < openAiStream.length; i++) {
      expect(parseAll([openAiStream.slice(0, i), openAiStream.slice(i)])).toEqual(expected);
    }
  });

  it("handles a stream fed one character at a time", () => {
    expect(parseAll(openAiStream.split(""))).toEqual(parseAll([openAiStream]));
  });

  it("joins a data line that is split mid-line across chunks", () => {
    const events = parseAll(['data: {"choices":[{"delta":{"con', 'tent":"x"}}]}', "\n\n"]);
    expect(events).toEqual([{ event: "message", data: '{"choices":[{"delta":{"content":"x"}}]}' }]);
  });

  it("parses the named RAG events", () => {
    const stream =
      'event: sources\ndata: [{"n":1,"path":"a.py","start_line":1,"end_line":60,"score":0.81}]\n\n' +
      'event: token\ndata: {"t":"See [1]"}\n\n' +
      'event: done\ndata: {"usage":{"completion_tokens":3}}\n\n';
    const events = parseAll(stream.match(/.{1,7}/gs) ?? []);
    expect(events.map((e) => e.event)).toEqual(["sources", "token", "done"]);
    expect(JSON.parse(events[1]!.data)).toEqual({ t: "See [1]" });
  });

  it("accepts CRLF and lone CR line endings, including CR and LF split across chunks", () => {
    const crlf = parseAll(["event: token\r", "\ndata: a\r\n\r", "\n"]);
    expect(crlf).toEqual([{ event: "token", data: "a" }]);
    const cr = parseAll(["data: a\rdata: b\r\r"]);
    expect(cr).toEqual([{ event: "message", data: "a\nb" }]);
  });

  it("joins multi-line data with newlines and strips only one leading space", () => {
    expect(parseAll(["data:  two spaces\ndata:none\n\n"])).toEqual([{ event: "message", data: " two spaces\nnone" }]);
  });

  it("ignores comments, keep-alives and unknown fields", () => {
    expect(parseAll([": ping\n\nfoo: bar\ndata: x\n\n"])).toEqual([{ event: "message", data: "x" }]);
  });

  it("does not dispatch an event without data and resets its type", () => {
    expect(parseAll(["event: token\n\ndata: y\n\n"])).toEqual([{ event: "message", data: "y" }]);
  });

  it("records id and retry fields", () => {
    expect(parseAll(["id: 7\nretry: 1500\ndata: z\n\n"])).toEqual([{ event: "message", data: "z", id: "7", retry: 1500 }]);
  });

  it("strips a leading byte order mark", () => {
    expect(parseAll(["﻿data: x\n\n"])).toEqual([{ event: "message", data: "x" }]);
  });

  it("flushes a final event that lacks the terminating blank line", () => {
    expect(parseAll(["data: last"])).toEqual([{ event: "message", data: "last" }]);
  });
});

describe("readSSE", () => {
  it("decodes multi-byte UTF-8 characters split across network chunks", async () => {
    const bytes = new TextEncoder().encode('data: {"t":"naïve → 日本"}\n\n');
    // Split inside the 3-byte arrow and inside a CJK character.
    const arrow = bytes.indexOf(0xe2);
    const parts = [bytes.slice(0, arrow + 1), bytes.slice(arrow + 1, arrow + 6), bytes.slice(arrow + 6)];
    const events = await collect(bytesStream(parts));
    expect(JSON.parse(events[0]!.data)).toEqual({ t: "naïve → 日本" });
  });

  it("yields events as they arrive across many small byte chunks", async () => {
    const bytes = new TextEncoder().encode(openAiStream);
    const parts: Uint8Array[] = [];
    for (let i = 0; i < bytes.length; i += 3) parts.push(bytes.slice(i, i + 3));
    const events = await collect(bytesStream(parts));
    expect(events).toHaveLength(4);
    expect(events[3]!.data).toBe("[DONE]");
  });
});
