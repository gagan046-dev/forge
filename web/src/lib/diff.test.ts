import { describe, expect, it } from "vitest";
import { diffLines, diffStats, extractCode, foldUnchanged } from "./diff";

describe("diffLines", () => {
  it("finds an inserted, removed and changed line", () => {
    const lines = diffLines("a\nb\nc\nd", "a\nB\nc\nd\ne");
    expect(lines).toEqual([
      { kind: "same", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "B" },
      { kind: "same", text: "c" },
      { kind: "same", text: "d" },
      { kind: "add", text: "e" },
    ]);
    expect(diffStats(lines)).toEqual({ added: 2, removed: 1 });
  });

  it("reports nothing for identical text", () => {
    expect(diffStats(diffLines("x\ny", "x\ny"))).toEqual({ added: 0, removed: 0 });
  });

  it("rebuilds both sides from the diff", () => {
    const before = "1\n2\n3\n4\n5\n6";
    const after = "0\n1\n3\n4\nfour\n6\n7";
    const lines = diffLines(before, after);
    expect(lines.filter((l) => l.kind !== "add").map((l) => l.text).join("\n")).toBe(before);
    expect(lines.filter((l) => l.kind !== "del").map((l) => l.text).join("\n")).toBe(after);
  });
});

describe("foldUnchanged", () => {
  it("folds long unchanged runs", () => {
    const before = Array.from({ length: 20 }, (_, i) => `l${i}`).join("\n");
    const after = before.replace("l10", "changed");
    const rows = foldUnchanged(diffLines(before, after), 2);
    expect(rows[0]).toEqual({ kind: "gap", count: 8 });
    expect(rows.at(-1)).toEqual({ kind: "gap", count: 7 });
    expect(rows.filter((r) => r.kind !== "gap")).toHaveLength(6);
  });
});

describe("extractCode", () => {
  it("takes the longest fenced block", () => {
    expect(extractCode("Here:\n```ts\nconst a = 1;\nconst b = 2;\n```\nand `x`\n```\nx\n```")).toBe("const a = 1;\nconst b = 2;\n");
  });
  it("falls back to the whole answer", () => {
    expect(extractCode("  print(1)  ")).toBe("print(1)\n");
  });
});
