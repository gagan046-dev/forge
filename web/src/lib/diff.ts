/**
 * Line diff for reviewing AI-proposed edits before applying them.
 *
 * Common prefix/suffix are trimmed first, then an LCS table covers the changed middle.
 * Files the console edits are small; a middle too big for the table is shown as one
 * delete + insert block, which is still a correct (if coarse) diff.
 */

export type DiffLine = { kind: "same" | "add" | "del"; text: string };

const MAX_CELLS = 4_000_000;

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }

  const out: DiffLine[] = a.slice(0, start).map((text) => ({ kind: "same", text }));
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);

  if ((midA.length + 1) * (midB.length + 1) > MAX_CELLS) {
    out.push(...midA.map((text) => ({ kind: "del" as const, text })), ...midB.map((text) => ({ kind: "add" as const, text })));
  } else {
    // lcs[i][j] = LCS length of midA[i:] and midB[j:]
    const cols = midB.length + 1;
    const lcs = new Uint32Array((midA.length + 1) * cols);
    for (let i = midA.length - 1; i >= 0; i--) {
      for (let j = midB.length - 1; j >= 0; j--) {
        lcs[i * cols + j] =
          midA[i] === midB[j] ? lcs[(i + 1) * cols + j + 1]! + 1 : Math.max(lcs[(i + 1) * cols + j]!, lcs[i * cols + j + 1]!);
      }
    }
    let i = 0;
    let j = 0;
    while (i < midA.length && j < midB.length) {
      if (midA[i] === midB[j]) {
        out.push({ kind: "same", text: midA[i]! });
        i++;
        j++;
      } else if (lcs[(i + 1) * cols + j]! >= lcs[i * cols + j + 1]!) {
        out.push({ kind: "del", text: midA[i++]! });
      } else {
        out.push({ kind: "add", text: midB[j++]! });
      }
    }
    while (i < midA.length) out.push({ kind: "del", text: midA[i++]! });
    while (j < midB.length) out.push({ kind: "add", text: midB[j++]! });
  }

  out.push(...a.slice(endA).map((text) => ({ kind: "same" as const, text })));
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.kind === "add") added++;
    else if (l.kind === "del") removed++;
  }
  return { added, removed };
}

export type DiffRow = DiffLine | { kind: "gap"; count: number };

/** Keeps `context` unchanged lines around each change and folds the rest into gaps. */
export function foldUnchanged(lines: DiffLine[], context = 3): DiffRow[] {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((l, idx) => {
    if (l.kind === "same") return;
    for (let k = Math.max(0, idx - context); k <= Math.min(lines.length - 1, idx + context); k++) keep[k] = true;
  });
  const rows: DiffRow[] = [];
  let gap = 0;
  lines.forEach((l, idx) => {
    if (keep[idx]) {
      if (gap) rows.push({ kind: "gap", count: gap });
      gap = 0;
      rows.push(l);
    } else {
      gap++;
    }
  });
  if (gap) rows.push({ kind: "gap", count: gap });
  return rows;
}

/**
 * The file content from a model answer: the longest fenced code block, or the whole
 * answer when the model ignored the "one code block" instruction.
 */
export function extractCode(answer: string): string {
  const blocks = [...answer.matchAll(/```[^\n`]*\n([\s\S]*?)\n?```/g)].map((m) => m[1]!);
  if (blocks.length === 0) return answer.trim() + "\n";
  const longest = blocks.reduce((best, b) => (b.length > best.length ? b : best));
  return longest.endsWith("\n") ? longest : `${longest}\n`;
}
