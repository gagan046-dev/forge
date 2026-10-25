import type { DiffRow } from "../lib/diff";

/** A folded line diff: + / − lines with context, long unchanged runs collapsed. */
export function DiffView({ rows }: { rows: DiffRow[] }) {
  return (
    <pre className="diff">
      {rows.map((r, i) =>
        r.kind === "gap" ? (
          <div key={i} className="diff-gap">
            ⋯ {r.count} unchanged line{r.count === 1 ? "" : "s"}
          </div>
        ) : (
          <div key={i} className={`diff-${r.kind}`}>
            <span className="diff-sign">{r.kind === "add" ? "+" : r.kind === "del" ? "−" : " "}</span>
            {r.text || " "}
          </div>
        ),
      )}
    </pre>
  );
}
