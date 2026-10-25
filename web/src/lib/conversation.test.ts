import { describe, expect, it } from "vitest";
import { toModelMessages, toStoredMessages, toTurns, type Turn } from "./conversation";

let n = 0;
const key = () => `k${++n}`;

const stats = { ttftMs: 467, durationMs: 3000, queueMs: 0, completionTokens: 123, tokensPerSecond: 49.4 };
const turns: Turn[] = [
  { key: "1", role: "user", content: "first" },
  { key: "2", role: "assistant", content: "answer", stats },
  { key: "3", role: "user", content: "fails" },
  { key: "4", role: "assistant", content: "", error: "Rate limit reached for this key. Retry in 12s." },
  { key: "5", role: "user", content: "stopped one" },
  { key: "6", role: "assistant", content: "partial", stopped: true },
];

describe("conversation storage", () => {
  it("keeps failed answers, stats and the stopped marker when saving", () => {
    const stored = toStoredMessages("be brief", turns);
    expect(stored.map((m) => m.role)).toEqual(["system", "user", "assistant", "user", "assistant", "user", "assistant"]);
    expect(stored[2]).toEqual({ role: "assistant", content: "answer", stats });
    expect(stored[4]).toEqual({ role: "assistant", content: "", error: "Rate limit reached for this key. Retry in 12s." });
    expect(stored[6]).toEqual({ role: "assistant", content: "partial", stopped: true });
  });

  it("restores the same turns when a conversation is reopened", () => {
    const back = toTurns(toStoredMessages("be brief", turns), key);
    expect(back.map(({ key: _k, ...rest }) => rest)).toEqual(turns.map(({ key: _k, ...rest }) => rest));
  });

  it("ignores malformed extra fields from the server", () => {
    const [turn] = toTurns([{ role: "assistant", content: "x", stopped: "yes" as unknown as boolean, error: "" }], key);
    expect(turn).toEqual({ key: expect.any(String), role: "assistant", content: "x" });
  });

  it("sends the model only answered exchanges, as plain role/content", () => {
    const next: Turn = { key: "7", role: "user", content: "next question" };
    expect(toModelMessages("be brief", [...turns, next])).toEqual([
      { role: "system", content: "be brief" },
      { role: "user", content: "first" },
      { role: "assistant", content: "answer" },
      { role: "user", content: "stopped one" },
      { role: "assistant", content: "partial" },
      { role: "user", content: "next question" },
    ]);
  });
});
