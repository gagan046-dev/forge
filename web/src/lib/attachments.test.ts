import { describe, expect, it } from "vitest";
import { composeMessage, isIgnoredPath, MAX_FILE_BYTES, MAX_TOTAL_CHARS, parseMessage, readAttachments } from "./attachments";

const file = (content: string, name = "f.txt") => new File([content], name);

describe("composeMessage / parseMessage", () => {
  it("round-trips text and files", () => {
    const atts = [
      { path: "src/a.ts", content: "const a = 1;\n" },
      { path: "README.md", content: "# hi" },
    ];
    const msg = composeMessage("explain this", atts);
    expect(msg.endsWith("\n\nexplain this")).toBe(true);
    const parsed = parseMessage(msg);
    expect(parsed.text).toBe("explain this");
    expect(parsed.attachments.map((a) => a.path)).toEqual(["src/a.ts", "README.md"]);
    expect(parsed.attachments[0]!.content).toBe("const a = 1;");
    expect(parsed.attachments[1]!.content).toBe("# hi");
  });

  it("leaves plain messages alone", () => {
    expect(composeMessage("hello", [])).toBe("hello");
    expect(parseMessage("hello <file path=\"x\">")).toEqual({ text: 'hello <file path="x">', attachments: [] });
  });

  it("keeps quotes and newlines out of the path attribute", () => {
    const msg = composeMessage("q", [{ path: 'a"b\nc', content: "x" }]);
    expect(parseMessage(msg).attachments[0]!.path).toBe("a_b_c");
  });
});

describe("readAttachments", () => {
  it("skips ignored folders, binaries, big files and enforces the budget", async () => {
    const { attachments, skipped } = await readAttachments(
      [
        { path: "app/main.py", file: file("print(1)") },
        { path: "app/node_modules/x.js", file: file("x") },
        { path: "app/logo.png", file: file("png") },
        { path: "app/data.txt", file: file("a\u0000b") },
        { path: "app/huge.txt", file: file("x".repeat(MAX_FILE_BYTES + 1)) },
        { path: "app/z1.txt", file: file("y".repeat(MAX_TOTAL_CHARS - 5)) },
      ],
      [],
    );
    expect(attachments.map((a) => a.path)).toEqual(["app/main.py"]);
    expect(Object.fromEntries(skipped.map((s) => [s.path, s.reason]))).toEqual({
      "app/node_modules/x.js": "ignored",
      "app/logo.png": "binary",
      "app/data.txt": "binary",
      "app/huge.txt": "too large",
      "app/z1.txt": "over budget",
    });
  });

  it("replaces a file attached twice", async () => {
    const first = await readAttachments([{ path: "a.txt", file: file("old") }], []);
    const second = await readAttachments([{ path: "a.txt", file: file("new") }], first.attachments);
    expect(second.attachments).toEqual([{ path: "a.txt", content: "new" }]);
  });

  it("knows ignored paths", () => {
    expect(isIgnoredPath(".git/config")).toBe(true);
    expect(isIgnoredPath("src/git.ts")).toBe(false);
  });
});
