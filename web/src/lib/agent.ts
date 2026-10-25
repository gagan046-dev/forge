/**
 * The editor agent: a chat that reads, searches and changes files in a workspace.
 *
 * Why a text protocol and not OpenAI tool calls: qwen2.5-coder:7b (forge-chat) does not
 * produce tool_calls reliably (it prints one JSON blob as text and stops), but it writes
 * files dependably inside simple tagged blocks, the approach Aider and bolt.new use for
 * small models. Measured: 0/2 tool-call runs vs 2/2 protocol runs for a 4-file scaffold.
 *
 * Reads and searches run immediately (their results go back to the model). Changes are
 * collected in a Changeset the user reviews and applies; reads see the pending changes,
 * so the model can build on what it just wrote.
 */

export type Action =
  | { kind: "write"; path: string; content: string }
  | { kind: "edit"; path: string; blocks: { search: string; replace: string }[] }
  | { kind: "mkdir"; path: string }
  | { kind: "delete"; path: string }
  | { kind: "move"; from: string; to: string }
  | { kind: "read"; path: string }
  | { kind: "search"; query: string };

export const AGENT_SYSTEM_PROMPT = `You are Forge Agent, a coding agent inside the user's project editor. You change the project ONLY with these action blocks:

<write path="path/file.ext">
complete file content
</write>
<edit path="path/file.ext">
<<<<<<< SEARCH
exact existing lines
=======
replacement lines
>>>>>>> REPLACE
</edit>
<mkdir path="folder"/>
<move from="old/path" to="new/path"/>
<delete path="path"/>
<read path="path/file.ext"/>
<search query="text"/>

Rules:
- First say in one or two sentences what you will do, then output the blocks.
- Code you show in a normal \`\`\` block is NOT saved. Every file you create or change MUST be inside a <write> or <edit> block.
- To change an existing file you have not seen, <read> it first and stop; its content comes back to you.
- Only <read> files from the project file list. Libraries and packages (sklearn, react, ...) are not project files.
- <write> holds the WHOLE file, never a snippet. For a small change in a big file use <edit>; SEARCH must match the file exactly.
- Writing a file creates its folders; use <mkdir> only for empty folders.
- Paths are relative to the project root. Never use ../ or absolute paths.
- To find code, use <search query="word"/> with ONE identifier or word that appears in the code (e.g. health, def main, API_KEY), not a sentence. Never ask the user to run grep or find.
- You cannot run commands. Write the code first; afterwards tell the user which commands to run (install, tests).
- When the task is done, end with a short summary and no read or search blocks.

Example. User: "Create hello.py that prints hi, with a test"
You:
I'll create hello.py and a pytest test for it.
<write path="hello.py">
def greet():
    return "hi"


if __name__ == "__main__":
    print(greet())
</write>
<write path="tests/test_hello.py">
from hello import greet


def test_greet():
    assert greet() == "hi"
</write>
Summary: created hello.py and tests/test_hello.py. Run: pip install pytest && pytest`;

// Double or single quotes: the model uses both.
const ATTR = (name: string) => `${name}\\s*=\\s*["']([^"'\\n]*)["']`;
// The model sometimes opens a block with ``` instead of <, so both are accepted.
const OPEN = "(?:<|```)";
const PATTERNS: { kind: Action["kind"]; re: RegExp }[] = [
  { kind: "write", re: new RegExp(`${OPEN}write\\s+${ATTR("path")}\\s*>\\n?([\\s\\S]*?)</write>`, "g") },
  { kind: "edit", re: new RegExp(`${OPEN}edit\\s+${ATTR("path")}\\s*>\\n?([\\s\\S]*?)</edit>`, "g") },
  // One-line actions: `<read path="x"/>`, but also `read path="x"` at the start of a line (the
  // model sometimes drops the "<" inside a code fence) and `<read>x</read>`.
  ...(["mkdir", "delete", "read"] as const).flatMap((kind) => [
    { kind, re: new RegExp(`(?:<|^[ \\t]*)${kind}\\s+${ATTR("path")}\\s*/?>?(?:</${kind}>)?`, "gm") },
    { kind, re: new RegExp(`<${kind}>\\s*([^<\\n]+?)\\s*</${kind}>`, "g") },
  ]),
  { kind: "move", re: new RegExp(`(?:<|^[ \\t]*)move\\s+${ATTR("from")}\\s+${ATTR("to")}\\s*/?>?(?:</move>)?`, "gm") },
  { kind: "search", re: new RegExp(`(?:<|^[ \\t]*)search\\s+${ATTR("query")}\\s*/?>?(?:</search>)?`, "gm") },
  { kind: "search", re: /<search>\s*([^<\n]+?)\s*<\/search>/g },
];

/**
 * "I will search…" or a code block with no action: the model described or showed the work
 * instead of doing it.
 */
export function announcedWithoutAction(text: string, actions: Action[], task = ""): boolean {
  if (actions.length > 0) return false;
  const asksForFiles = /\b(create|build|write|make|add|implement|generate|scaffold|project|file|code)\b/i.test(task);
  return (
    /\b(I will|I'll|Let me|I am going to|Let's|we will|we'll)\b[^.\n]*\b(search|read|look|create|write|edit|add|update|delete|move|build|start)\b/i.test(text) ||
    /\b(created|updated|added|wrote)\b[^\n]*\.\w+/i.test(text) ||
    // A plain code block is fine as an answer to a question, not to "build this".
    (asksForFiles && /```[a-z]*\n[\s\S]{40,}?```/.test(text))
  );
}

export const NUDGE =
  "Nothing was saved: you did not output any action block. Do the work now. Put every file inside <write path=\"...\">...</write> " +
  "(code in a normal ``` block is not saved), or use <search query=\"word\"/> / <read path=\"...\"/> to look first.";

/** Removes a ``` fence the model wrapped around a whole file inside <write>. */
function unfence(content: string): string {
  const m = /^```[^\n]*\n([\s\S]*?)\n?```\s*$/.exec(content);
  const body = m ? m[1]! : content;
  return body.endsWith("\n") || body === "" ? body : `${body}\n`;
}

function parseEditBlocks(body: string): { search: string; replace: string }[] {
  const re = /<{5,}\s*SEARCH\n([\s\S]*?)\n?={5,}\n([\s\S]*?)\n?>{5,}\s*REPLACE/g;
  return [...body.matchAll(re)].map((m) => ({ search: m[1]!, replace: m[2]! }));
}

const cleanPath = (p: string) => p.trim().replace(/^\.\//, "").replace(/^\/+/, "");

/** All actions in the order they appear in the answer. */
export function parseActions(text: string): Action[] {
  const found: { at: number; action: Action }[] = [];
  for (const { kind, re } of PATTERNS) {
    for (const m of text.matchAll(re)) {
      const at = m.index ?? 0;
      let action: Action;
      if (kind === "write") action = { kind, path: cleanPath(m[1]!), content: unfence(m[2]!) };
      else if (kind === "edit") action = { kind, path: cleanPath(m[1]!), blocks: parseEditBlocks(m[2]!) };
      else if (kind === "move") action = { kind, from: cleanPath(m[1]!), to: cleanPath(m[2]!) };
      else if (kind === "search") action = { kind, query: m[1]! };
      else action = { kind, path: cleanPath(m[1]!) };
      found.push({ at, action });
    }
  }
  return found.sort((a, b) => a.at - b.at).map((f) => f.action);
}

const FILE_NAME = /[\w.-]*[\w-]\.(?:py|ipynb|js|mjs|cjs|ts|tsx|jsx|json|md|txt|ya?ml|toml|ini|cfg|html|css|scss|sh|ps1|sql|go|rs|java|kt|rb|php|c|h|cpp|hpp|cs|env|gitignore)|Dockerfile|Makefile|Procfile/;
const PATH_AT_END = new RegExp(`[\`*"'(]*((?:[\\w.-]+/)*(?:${FILE_NAME.source}))[\`*"')]*\\s*:?\\s*$`);
const PATH_COMMENT = new RegExp(`^\\s*(?:#|//|--|<!--)\\s*((?:[\\w.-]+/)*(?:${FILE_NAME.source}))\\s*(?:-->)?\\s*$`);

/**
 * Fallback for answers with no write/edit blocks: the 7B model sometimes writes
 * "Created main.py:" and then a normal ``` block. A fence counts as a file when the line just
 * before it ends with a file name, or when its first line is a comment naming one.
 * These still go through the review, so a wrong guess is only a proposal.
 */
export function salvageWrites(text: string): Action[] {
  const out: Action[] = [];
  const seen = new Set<string>();
  // Group 2 (when present) is the line right above the fence, group 1 the one before it.
  const fence = /(?:^|\n)([^\n]*)\n(?:([^\n]*)\n)?```[^\n]*\n([\s\S]*?)\n```/g;
  for (const m of text.matchAll(fence)) {
    const body = m[3]!;
    const near = (m[2] ?? m[1]!).trim();
    const fromLine = PATH_AT_END.exec(near)?.[1] ?? (m[2] !== undefined ? PATH_AT_END.exec(m[1]!.trim())?.[1] : undefined);
    const fromComment = PATH_COMMENT.exec(body.split("\n")[0] ?? "")?.[1];
    const path = fromLine ?? fromComment;
    if (!path || seen.has(path)) continue;
    seen.add(path);
    out.push({ kind: "write", path: cleanPath(path), content: body.endsWith("\n") ? body : `${body}\n` });
  }
  return out;
}

/** The answer's actions, falling back to salvaged file blocks when it has no write/edit. */
export function actionsFor(text: string): { actions: Action[]; salvaged: boolean } {
  const actions = parseActions(text);
  if (actions.some((a) => a.kind === "write" || a.kind === "edit")) return { actions, salvaged: false };
  const salvaged = salvageWrites(text);
  return salvaged.length ? { actions: [...actions, ...salvaged], salvaged: true } : { actions, salvaged: false };
}

/** Actions back in protocol form, so the chat history always shows the model the right format. */
export function serializeActions(actions: Action[]): string {
  return actions
    .map((a) => {
      switch (a.kind) {
        case "write":
          return `<write path="${a.path}">\n${a.content}</write>`;
        case "edit":
          return `<edit path="${a.path}">\n${a.blocks.map((b) => `<<<<<<< SEARCH\n${b.search}\n=======\n${b.replace}\n>>>>>>> REPLACE`).join("\n")}\n</edit>`;
        case "move":
          return `<move from="${a.from}" to="${a.to}"/>`;
        case "search":
          return `<search query="${a.query}"/>`;
        default:
          return `<${a.kind} path="${a.path}"/>`;
      }
    })
    .join("\n");
}

/** The answer without its action blocks: what the chat shows as the agent's words. */
export function narrative(text: string): string {
  let out = text;
  for (const { re } of PATTERNS) out = out.replace(re, "");
  return out.replace(/```\s*```/g, "").replace(/\n{3,}/g, "\n\n").trim();
}

// ---------------------------------------------------------------------------- changeset

export interface FileChange {
  path: string;
  /** null: the file is new. */
  before: string | null;
  /** null: the file is deleted. */
  after: string | null;
}

export interface Changeset {
  files: Map<string, FileChange>;
  folders: Set<string>;
  deletedFolders: Set<string>;
}

export const emptyChangeset = (): Changeset => ({ files: new Map(), folders: new Set(), deletedFolders: new Set() });

export const changeCount = (c: Changeset) => c.files.size + c.folders.size + c.deletedFolders.size;

/** How the agent reaches the workspace. Injected, so the logic is testable without a server. */
export interface AgentIO {
  /** File content as the user sees it (open editor buffer included); null when missing. */
  read(path: string): Promise<string | null>;
  isDir(path: string): boolean;
  search(query: string): Promise<{ path: string; line: number; text: string }[]>;
}

export interface StepLog {
  label: string;
  ok: boolean;
}

const MAX_RESULT_CHARS = 6000;

function safeRel(path: string): boolean {
  return !!path && !path.includes("..") && !path.includes("\\") && !path.startsWith("/") && !/(^|\/)\.git(\/|$)/.test(path);
}

/** The current content of a file, pending changes first. */
async function current(cs: Changeset, io: AgentIO, path: string): Promise<string | null> {
  const pending = cs.files.get(path);
  if (pending) return pending.after;
  if ([...cs.deletedFolders].some((d) => path.startsWith(`${d}/`))) return null;
  return io.read(path);
}

async function record(cs: Changeset, io: AgentIO, path: string, after: string | null) {
  const existing = cs.files.get(path);
  const before = existing ? existing.before : await io.read(path);
  if (before === after || (before === null && after === null)) cs.files.delete(path);
  else cs.files.set(path, { path, before, after });
}

export function applyEdit(content: string, blocks: { search: string; replace: string }[]): { content: string; failed: string[] } {
  const failed: string[] = [];
  let out = content;
  for (const b of blocks) {
    if (b.search && out.includes(b.search)) out = out.replace(b.search, b.replace);
    else if (b.search.trim() && out.includes(b.search.trim())) out = out.replace(b.search.trim(), b.replace.trim());
    else failed.push(b.search.split("\n")[0] ?? "");
  }
  return { content: out, failed };
}

/**
 * Runs one answer's actions against the changeset. Returns what goes back to the model
 * (read/search results and failures) and a log line per action for the chat.
 */
export async function runActions(actions: Action[], cs: Changeset, io: AgentIO): Promise<{ results: string[]; log: StepLog[] }> {
  const results: string[] = [];
  const log: StepLog[] = [];
  const fail = (label: string, why: string) => {
    log.push({ label: `${label} — ${why}`, ok: false });
    results.push(`${label} failed: ${why}`);
  };

  for (const a of actions) {
    const paths = a.kind === "move" ? [a.from, a.to] : a.kind === "search" ? [] : [a.path];
    const label = a.kind === "move" ? `move ${a.from} → ${a.to}` : a.kind === "search" ? `search "${a.query}"` : `${a.kind} ${a.path}`;
    if (paths.some((p) => !safeRel(p))) {
      fail(label, "path must be relative to the project, without .. or .git");
      continue;
    }
    switch (a.kind) {
      case "read": {
        const text = io.isDir(a.path) ? null : await current(cs, io, a.path);
        if (text === null) {
          fail(label, "not a file in this project (only files in the project list can be read; libraries need no reading)");
          break;
        }
        const shown = text.length > MAX_RESULT_CHARS ? `${text.slice(0, MAX_RESULT_CHARS)}\n… (truncated, ${text.length} chars)` : text;
        results.push(`<file path="${a.path}">\n${shown}\n</file>`);
        log.push({ label, ok: true });
        break;
      }
      case "search": {
        let hits = await io.search(a.query);
        // The model often searches for a phrase ("health route"); fall back to its longest word.
        const words = a.query.split(/\W+/).filter((w) => w.length >= 3).sort((x, y) => y.length - x.length);
        if (hits.length === 0 && words.length > 1) hits = await io.search(words[0]!);
        results.push(
          hits.length === 0
            ? `search "${a.query}": no matches`
            : `search "${a.query}":\n${hits.slice(0, 40).map((h) => `${h.path}:${h.line}: ${h.text}`).join("\n")}`,
        );
        log.push({ label: `${label} · ${hits.length} hit${hits.length === 1 ? "" : "s"}`, ok: true });
        break;
      }
      case "write":
        if (io.isDir(a.path)) {
          fail(label, "that path is a folder");
          break;
        }
        await record(cs, io, a.path, a.content);
        log.push({ label, ok: true });
        break;
      case "edit": {
        const text = await current(cs, io, a.path);
        if (text === null) {
          fail(label, "no such file (use <write> to create it)");
          break;
        }
        if (a.blocks.length === 0) {
          fail(label, "no SEARCH/REPLACE block found");
          break;
        }
        const { content, failed } = applyEdit(text, a.blocks);
        if (failed.length > 0) {
          fail(label, `SEARCH text not found: "${failed[0]}". Read the file and try again`);
          break;
        }
        await record(cs, io, a.path, content);
        log.push({ label, ok: true });
        break;
      }
      case "mkdir":
        cs.folders.add(a.path);
        log.push({ label, ok: true });
        break;
      case "delete":
        if (io.isDir(a.path)) {
          cs.deletedFolders.add(a.path);
          log.push({ label, ok: true });
        } else if ((await current(cs, io, a.path)) === null) {
          fail(label, "no such file");
        } else {
          await record(cs, io, a.path, null);
          log.push({ label, ok: true });
        }
        break;
      case "move": {
        if (io.isDir(a.from)) {
          fail(label, "only files can be moved; write the new files and delete the old folder");
          break;
        }
        const text = await current(cs, io, a.from);
        if (text === null) {
          fail(label, "no such file");
          break;
        }
        await record(cs, io, a.to, text);
        await record(cs, io, a.from, null);
        log.push({ label, ok: true });
        break;
      }
    }
  }
  return { results, log };
}

/** True when the model asked for information (or must retry) and should get another turn. */
export function needsFollowUp(actions: Action[], results: string[]): boolean {
  return actions.some((a) => a.kind === "read" || a.kind === "search") || results.some((r) => r.includes(" failed: "));
}

/** The project overview sent with each user message: the file tree with pending changes applied. */
export function describeProject(paths: string[], cs: Changeset, limit = 300): string {
  const all = new Set(paths);
  for (const [p, change] of cs.files) {
    if (change.after === null) all.delete(p);
    else all.add(p);
  }
  for (const d of cs.deletedFolders) for (const p of [...all]) if (p.startsWith(`${d}/`)) all.delete(p);
  const sorted = [...all].sort();
  if (sorted.length === 0) return "Project files: (empty project)";
  const shown = sorted.slice(0, limit);
  return `Project files (${sorted.length}):\n${shown.join("\n")}${sorted.length > limit ? `\n… and ${sorted.length - limit} more` : ""}`;
}
