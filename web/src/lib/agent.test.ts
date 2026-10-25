import { describe, expect, it } from "vitest";
import { announcedWithoutAction, applyEdit, describeProject, emptyChangeset, narrative, needsFollowUp, parseActions, runActions, type AgentIO } from "./agent";

function fakeIO(files: Record<string, string>, dirs: string[] = []): AgentIO {
  return {
    read: async (p) => files[p] ?? null,
    isDir: (p) => dirs.includes(p),
    search: async (q) =>
      Object.entries(files).flatMap(([path, text]) =>
        text.split("\n").flatMap((line, i) => (line.includes(q) ? [{ path, line: i + 1, text: line }] : [])),
      ),
  };
}

describe("parseActions", () => {
  it("reads every block kind in order, including the ``` opener the 7B model sometimes uses", () => {
    const text = [
      "I will create the app.",
      '```write path="app.py">',
      "print(1)",
      "</write>",
      '<mkdir path="static"/>',
      '<read path="./README.md"/>',
      '<search query="def main"/>',
      '<move from="a.txt" to="b.txt"/>',
      '<delete path="old.py"></delete>',
      '<edit path="x.py">',
      "<<<<<<< SEARCH",
      "a = 1",
      "=======",
      "a = 2",
      ">>>>>>> REPLACE",
      "</edit>",
    ].join("\n");
    expect(parseActions(text)).toEqual([
      { kind: "write", path: "app.py", content: "print(1)\n" },
      { kind: "mkdir", path: "static" },
      { kind: "read", path: "README.md" },
      { kind: "search", query: "def main" },
      { kind: "move", from: "a.txt", to: "b.txt" },
      { kind: "delete", path: "old.py" },
      { kind: "edit", path: "x.py", blocks: [{ search: "a = 1", replace: "a = 2" }] },
    ]);
    expect(narrative(text)).toBe("I will create the app.");
  });

  it("unwraps a code fence inside <write>", () => {
    expect(parseActions('<write path="a.js">\n```js\nlet a;\n```\n</write>')).toEqual([{ kind: "write", path: "a.js", content: "let a;\n" }]);
  });
});

describe("runActions", () => {
  it("collects writes as pending changes and lets later reads see them", async () => {
    const cs = emptyChangeset();
    const io = fakeIO({ "app.py": "old\n" });
    const { results, log } = await runActions(
      parseActions('<write path="app.py">\nnew\n</write>\n<write path="src/util.py">\nx = 1\n</write>\n<read path="app.py"/>'),
      cs,
      io,
    );
    expect(cs.files.get("app.py")).toEqual({ path: "app.py", before: "old\n", after: "new\n" });
    expect(cs.files.get("src/util.py")).toEqual({ path: "src/util.py", before: null, after: "x = 1\n" });
    expect(results).toEqual(['<file path="app.py">\nnew\n\n</file>']);
    expect(log.every((l) => l.ok)).toBe(true);
  });

  it("applies edits, reports a SEARCH miss back to the model, and refuses unsafe paths", async () => {
    const cs = emptyChangeset();
    const io = fakeIO({ "x.py": "a = 1\nb = 2\n" });
    const ok = await runActions([{ kind: "edit", path: "x.py", blocks: [{ search: "b = 2", replace: "b = 3" }] }], cs, io);
    expect(ok.results).toEqual([]);
    expect(cs.files.get("x.py")!.after).toBe("a = 1\nb = 3\n");
    const miss = await runActions(
      [
        { kind: "edit", path: "x.py", blocks: [{ search: "zzz", replace: "y" }] },
        { kind: "write", path: "../etc/passwd", content: "x" },
        { kind: "write", path: ".git/config", content: "x" },
      ],
      cs,
      io,
    );
    expect(miss.log.map((l) => l.ok)).toEqual([false, false, false]);
    expect(needsFollowUp([], miss.results)).toBe(true);
  });

  it("moves and deletes files, deletes folders, and drops a write that restores the original", async () => {
    const cs = emptyChangeset();
    const io = fakeIO({ "a.txt": "A\n", "lib/x.py": "x\n" }, ["lib"]);
    await runActions(parseActions('<move from="a.txt" to="docs/a.txt"/>\n<delete path="lib"/>'), cs, io);
    expect(cs.files.get("docs/a.txt")).toEqual({ path: "docs/a.txt", before: null, after: "A\n" });
    expect(cs.files.get("a.txt")).toEqual({ path: "a.txt", before: "A\n", after: null });
    expect([...cs.deletedFolders]).toEqual(["lib"]);
    expect(describeProject(["a.txt", "lib/x.py"], cs)).toBe("Project files (1):\ndocs/a.txt");
    await runActions(parseActions('<write path="a.txt">\nA\n</write>'), cs, io);
    expect(cs.files.has("a.txt")).toBe(false);
  });

  it("returns search hits", async () => {
    const { results } = await runActions([{ kind: "search", query: "def" }], emptyChangeset(), fakeIO({ "m.py": "x\ndef main():\n" }));
    expect(results).toEqual(['search "def":\nm.py:2: def main():']);
  });
});

describe("search fallback and quoting", () => {
  it("accepts single quotes and retries a phrase with its longest word", async () => {
    const actions = parseActions("<search query='health route'/>");
    expect(actions).toEqual([{ kind: "search", query: "health route" }]);
    const { results } = await runActions(actions, emptyChangeset(), fakeIO({ "app.py": "@app.route('/health')\ndef health():\n" }));
    expect(results[0]).toContain("app.py:1:");
  });
});

describe("formats seen from the 7B model", () => {
  it("parses actions without '<' inside a fence and tag-body forms", () => {
    expect(parseActions('Run:\n```sh\nsearch query="health"\n```')).toEqual([{ kind: "search", query: "health" }]);
    expect(parseActions("<search>health</search> then <read>app.py</read>")).toEqual([
      { kind: "search", query: "health" },
      { kind: "read", path: "app.py" },
    ]);
    expect(parseActions("We search query results in the UI.")).toEqual([]);
  });

  it("spots an announced action with no block", () => {
    expect(announcedWithoutAction("I will search for the text health in the project.", [])).toBe(true);
    expect(announcedWithoutAction("The route is in app.py line 6.", [])).toBe(false);
    expect(announcedWithoutAction("I will search.", [{ kind: "search", query: "x" }])).toBe(false);
  });
});

describe("applyEdit", () => {
  it("tolerates surrounding whitespace in SEARCH", () => {
    expect(applyEdit("x = 1\n", [{ search: "x = 1   ", replace: "x = 2 " }]).content).toBe("x = 2\n");
  });
});
