/**
 * Incremental Server-Sent Events parser (WHATWG "event stream interpretation").
 *
 * Network chunks can end anywhere: mid-line, mid-field, between a CR and its LF,
 * or inside a multi-byte UTF-8 sequence. The parser therefore keeps the
 * unterminated tail of the previous chunk and only acts on complete lines.
 * Byte decoding lives in readSSE(), which uses a streaming TextDecoder.
 */

export interface SSEEvent {
  /** Event type; "message" when the server sent no `event:` field. */
  event: string;
  data: string;
  id?: string;
  retry?: number;
}

export class SSEParser {
  private buffer = "";
  private started = false;
  // A chunk that ends in "\r" may be followed by "\n" in the next chunk; that
  // "\n" belongs to the same line ending and must not produce an empty line.
  private swallowLeadingLF = false;

  private dataLines: string[] = [];
  private eventType = "";
  private lastEventId: string | undefined;
  private retry: number | undefined;

  feed(chunk: string): SSEEvent[] {
    if (chunk.length === 0) return [];
    if (!this.started) {
      this.started = true;
      if (chunk.charCodeAt(0) === 0xfeff) chunk = chunk.slice(1);
    }
    if (this.swallowLeadingLF) {
      this.swallowLeadingLF = false;
      if (chunk.startsWith("\n")) chunk = chunk.slice(1);
    }

    const text = this.buffer + chunk;
    const events: SSEEvent[] = [];
    let lineStart = 0;

    for (let i = 0; i < text.length; i++) {
      const c = text.charCodeAt(i);
      if (c !== 10 && c !== 13) continue;

      const line = text.slice(lineStart, i);
      if (c === 13) {
        if (i + 1 < text.length) {
          if (text.charCodeAt(i + 1) === 10) i++;
        } else {
          this.swallowLeadingLF = true;
        }
      }
      lineStart = i + 1;
      this.processLine(line, events);
    }

    this.buffer = text.slice(lineStart);
    return events;
  }

  /**
   * Call when the stream ends. The spec discards an event that was not
   * terminated by a blank line, but several servers omit the final blank line,
   * so a complete trailing event is still delivered rather than silently lost.
   */
  end(): SSEEvent[] {
    const events: SSEEvent[] = [];
    if (this.buffer.length > 0) {
      this.processLine(this.buffer, events);
      this.buffer = "";
    }
    this.dispatch(events);
    return events;
  }

  private processLine(line: string, events: SSEEvent[]): void {
    if (line === "") {
      this.dispatch(events);
      return;
    }
    if (line.startsWith(":")) return; // comment / keep-alive

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    switch (field) {
      case "data":
        this.dataLines.push(value);
        break;
      case "event":
        this.eventType = value;
        break;
      case "id":
        if (!value.includes("\0")) this.lastEventId = value;
        break;
      case "retry":
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
      default:
        break; // unknown fields are ignored per spec
    }
  }

  private dispatch(events: SSEEvent[]): void {
    if (this.dataLines.length === 0) {
      this.eventType = "";
      return;
    }
    const event: SSEEvent = {
      event: this.eventType || "message",
      data: this.dataLines.join("\n"),
    };
    if (this.lastEventId !== undefined) event.id = this.lastEventId;
    if (this.retry !== undefined) event.retry = this.retry;
    events.push(event);
    this.dataLines = [];
    this.eventType = "";
  }
}

/** Reads a fetch() response body as a sequence of SSE events. */
export async function* readSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<SSEEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = new SSEParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* parser.feed(decoder.decode(value, { stream: true }));
    }
    yield* parser.feed(decoder.decode());
    yield* parser.end();
  } finally {
    // Cancelling releases the connection if the consumer stops early (e.g. on [DONE]).
    reader.cancel().catch(() => undefined);
  }
}
