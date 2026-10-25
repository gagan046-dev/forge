/**
 * Files and folders attached to a chat message.
 *
 * Attachments are read in the browser and sent as text inside the user message, so the
 * gateway and the stored conversation need no new fields:
 *
 *   <file path="src/app.ts">
 *   ...file content...
 *   </file>
 *
 *   the question
 *
 * The chat context is small (8k tokens for the 7B model), so there is a total budget and
 * folders skip dependency/build directories and binary files.
 */

export interface Attachment {
  path: string;
  content: string;
}

export interface SkippedFile {
  path: string;
  reason: "binary" | "too large" | "over budget" | "ignored";
}

/** One file larger than this is never useful context for a small model. */
export const MAX_FILE_BYTES = 100_000;
/** Roughly 6k tokens: leaves room for the question and the answer in an 8k context. */
export const MAX_TOTAL_CHARS = 24_000;

const IGNORED_DIRS = new Set([
  ".git", "node_modules", "dist", "build", "out", ".next", ".venv", "venv", "__pycache__",
  ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", "vendor", ".idea", ".vscode", "coverage",
]);
const BINARY_EXT = /\.(png|jpe?g|gif|webp|ico|bmp|pdf|zip|gz|tgz|tar|7z|rar|exe|dll|so|dylib|bin|class|jar|woff2?|ttf|otf|mp[34]|mov|avi|wasm|pyc|lock)$/i;

/** True if a path goes through a directory that is never worth attaching. */
export function isIgnoredPath(path: string): boolean {
  return path.split("/").slice(0, -1).some((part) => IGNORED_DIRS.has(part));
}

export function looksBinary(text: string): boolean {
  return text.slice(0, 8000).includes("\u0000");
}

export function totalChars(attachments: Attachment[]): number {
  return attachments.reduce((sum, a) => sum + a.content.length, 0);
}

/** A file picked by the user, with the path to show (folder-relative for folder picks). */
export interface PickedFile {
  path: string;
  file: File;
}

/**
 * Reads picked files into attachments, adding to `existing` (same path = replaced),
 * and reports what was left out and why.
 */
export async function readAttachments(
  picked: PickedFile[],
  existing: Attachment[],
): Promise<{ attachments: Attachment[]; skipped: SkippedFile[] }> {
  const byPath = new Map(existing.map((a) => [a.path, a]));
  const skipped: SkippedFile[] = [];
  const sorted = [...picked].sort((a, b) => a.path.localeCompare(b.path));
  for (const { path, file } of sorted) {
    if (isIgnoredPath(path)) {
      skipped.push({ path, reason: "ignored" });
      continue;
    }
    if (BINARY_EXT.test(path)) {
      skipped.push({ path, reason: "binary" });
      continue;
    }
    if (file.size > MAX_FILE_BYTES) {
      skipped.push({ path, reason: "too large" });
      continue;
    }
    const content = await file.text();
    if (looksBinary(content)) {
      skipped.push({ path, reason: "binary" });
      continue;
    }
    const used = totalChars([...byPath.values()]) - (byPath.get(path)?.content.length ?? 0);
    if (used + content.length > MAX_TOTAL_CHARS) {
      skipped.push({ path, reason: "over budget" });
      continue;
    }
    byPath.set(path, { path, content });
  }
  return { attachments: [...byPath.values()], skipped };
}

/** Collects files (recursively for folders) from a drag-and-drop. */
export async function filesFromDrop(items: DataTransferItemList): Promise<PickedFile[]> {
  const entries = [...items]
    .map((item) => (item.kind === "file" ? item.webkitGetAsEntry() : null))
    .filter((e): e is FileSystemEntry => e !== null);
  const out: PickedFile[] = [];

  const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
      out.push({ path, file });
    } else if (entry.isDirectory && !IGNORED_DIRS.has(entry.name)) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      // readEntries returns results in batches until it returns an empty list.
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (batch.length === 0) break;
        for (const child of batch) await walk(child, path);
      }
    }
  };

  for (const entry of entries) await walk(entry, "");
  return out;
}

/** Files from an <input type="file">, using the folder-relative path for folder picks. */
export function filesFromInput(list: FileList | null): PickedFile[] {
  return [...(list ?? [])].map((file) => ({ path: file.webkitRelativePath || file.name, file }));
}

const safePath = (path: string) => path.replace(/["\n\r]/g, "_");

/** The user message sent to the model and stored in the conversation. */
export function composeMessage(text: string, attachments: Attachment[]): string {
  if (attachments.length === 0) return text;
  const blocks = attachments.map((a) => `<file path="${safePath(a.path)}">\n${a.content.replace(/\n?$/, "\n")}</file>`);
  return `${blocks.join("\n\n")}\n\n${text}`;
}

const FILE_BLOCK = /^<file path="([^"\n]*)">\n([\s\S]*?)\n?<\/file>(?:\n\n|$)/;

/** Splits a stored user message back into the typed text and its attachments, for display. */
export function parseMessage(content: string): { text: string; attachments: Attachment[] } {
  const attachments: Attachment[] = [];
  let rest = content;
  for (let m = FILE_BLOCK.exec(rest); m; m = FILE_BLOCK.exec(rest)) {
    attachments.push({ path: m[1]!, content: m[2]! });
    rest = rest.slice(m[0].length);
  }
  return { text: rest, attachments };
}
