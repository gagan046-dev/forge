import { Children, isValidElement, memo, useMemo, type ReactElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { highlight } from "../lib/highlight";
import { CopyButton } from "./ui";

export function CodeBlock({ language, code, title }: { language: string | null; code: string; title?: string }) {
  const html = useMemo(() => highlight(code, language), [code, language]);
  return (
    <div className="codeblock">
      <div className="codeblock-bar">
        <span className="codeblock-lang">{title ?? language ?? "text"}</span>
        <CopyButton text={code} />
      </div>
      {/* highlight.js escapes the source; its output contains only <span class> markup. */}
      <pre>
        <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

/**
 * Turns "[3]" and "[1, 4]" citations into "#cite-n" links, skipping fenced and
 * inline code so array indexing like `xs[1]` stays untouched.
 */
export function linkCitations(markdown: string, valid: ReadonlySet<number>): string {
  const parts = markdown.split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) return part; // code segment
      return part.replace(/\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\](?![(:])/g, (whole, list: string) => {
        const nums = list.split(",").map((s) => Number(s.trim()));
        if (!nums.every((n) => valid.has(n))) return whole;
        return nums.map((n) => `[\\[${n}\\]](#cite-${n})`).join("");
      });
    })
    .join("");
}

interface MarkdownProps {
  content: string;
  /** When set, "[n]" citations become buttons that call onCite(n). */
  citations?: { valid: ReadonlySet<number>; onCite: (n: number) => void };
}

function MarkdownImpl({ content, citations }: MarkdownProps) {
  const source = citations ? linkCitations(content, citations.valid) : content;

  const components = useMemo<Components>(
    () => ({
      pre({ children }) {
        const child = Children.toArray(children)[0];
        if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) return <pre>{children}</pre>;
        const match = /language-([\w+#.-]+)/.exec(child.props.className ?? "");
        const code = textOf(child.props.children).replace(/\n$/, "");
        return <CodeBlock language={match?.[1]?.toLowerCase() ?? null} code={code} />;
      },
      a({ href, children }) {
        const cite = href?.startsWith("#cite-") ? Number(href.slice(6)) : null;
        if (cite !== null && citations) {
          return (
            <button type="button" className="cite" onClick={() => citations.onCite(cite)} title={`Show source ${cite}`}>
              {cite}
            </button>
          );
        }
        return (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      },
      table({ children }) {
        return (
          <div className="md-table">
            <table>{children}</table>
          </div>
        );
      },
    }),
    [citations],
  );

  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl) as (props: MarkdownProps) => ReactElement;
