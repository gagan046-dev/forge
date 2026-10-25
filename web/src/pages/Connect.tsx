import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { listOpenAIModels } from "../api/endpoints";
import type { OpenAIModel } from "../api/types";
import { CodeBlock } from "../components/Markdown";
import { CopyButton, PageHeader } from "../components/ui";

type Tab = "python" | "curl" | "continue";

const TABS: { id: Tab; label: string }[] = [
  { id: "python", label: "Python (OpenAI SDK)" },
  { id: "curl", label: "curl" },
  { id: "continue", label: "VS Code (Continue)" },
];

function pick(models: OpenAIModel[], role: string, fallback: string): string {
  return models.find((m) => m.role === role)?.id ?? fallback;
}

export function ConnectPage() {
  const [tab, setTab] = useState<Tab>("python");
  const [models, setModels] = useState<OpenAIModel[]>([]);
  const base = `${window.location.origin}/v1`;

  useEffect(() => {
    const controller = new AbortController();
    listOpenAIModels(controller.signal).then(setModels).catch(() => undefined);
    return () => controller.abort();
  }, []);

  const chat = pick(models, "chat", "forge-chat");
  const autocomplete = pick(models, "autocomplete", "forge-autocomplete");
  const embed = pick(models, "embedding", "forge-embed");

  const snippets = useMemo<Record<Tab, { lang: string; file?: string; code: string }>>(
    () => ({
      python: {
        lang: "python",
        file: "pip install openai",
        code: `import os
from openai import OpenAI

client = OpenAI(base_url="${base}", api_key=os.environ["FORGE_API_KEY"])

# Streaming chat
stream = client.chat.completions.create(
    model="${chat}",
    messages=[{"role": "user", "content": "Explain Python's GIL in two sentences."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()

# Embeddings
vectors = client.embeddings.create(model="${embed}", input=["def add(a, b): return a + b"])
print(len(vectors.data[0].embedding))
`,
      },
      curl: {
        lang: "bash",
        code: `export FORGE_API_KEY=fk_...   # from the API keys page

curl ${base}/models -H "Authorization: Bearer $FORGE_API_KEY"

curl -N ${base}/chat/completions \\
  -H "Authorization: Bearer $FORGE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${chat}", "stream": true,
       "messages": [{"role": "user", "content": "Write a haiku about GPUs."}]}'

# Fill-in-the-middle completion, the request autocomplete makes
curl ${base}/completions \\
  -H "Authorization: Bearer $FORGE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${autocomplete}", "prompt": "def fib(n):\\n    ", "suffix": "\\n\\nprint(fib(10))", "max_tokens": 64, "temperature": 0}'
`,
      },
      continue: {
        lang: "yaml",
        file: "~/.continue/config.yaml",
        code: `name: Forge
version: 1.0.0
schema: v1
models:
  - name: Forge chat
    provider: openai
    model: ${chat}
    apiBase: ${base}
    apiKey: \${{ secrets.FORGE_API_KEY }}
    roles: [chat, edit, apply]
  - name: Forge autocomplete
    provider: openai
    model: ${autocomplete}
    apiBase: ${base}
    apiKey: \${{ secrets.FORGE_API_KEY }}
    roles: [autocomplete]
    autocompleteOptions:
      debounceDelay: 250
      maxPromptTokens: 1024
  - name: Forge embeddings
    provider: openai
    model: ${embed}
    apiBase: ${base}
    apiKey: \${{ secrets.FORGE_API_KEY }}
    roles: [embed]
`,
      },
    }),
    [base, chat, autocomplete, embed],
  );

  const current = snippets[tab];

  return (
    <div className="page">
      <PageHeader
        title="Connect"
        description={
          <>
            Forge speaks the OpenAI API at <code>{base}</code>. Create a key on the <Link to="/keys">API keys</Link> page and
            export it as <code>FORGE_API_KEY</code>.
          </>
        }
      />

      <dl className="kv">
        <dt>Base URL</dt>
        <dd>
          <code>{base}</code> <CopyButton text={base} />
        </dd>
        <dt>Chat model</dt>
        <dd className="mono">{chat}</dd>
        <dt>Autocomplete model</dt>
        <dd className="mono">{autocomplete} <span className="muted">(FIM via /v1/completions with suffix)</span></dd>
        <dt>Embedding model</dt>
        <dd className="mono">{embed}</dd>
      </dl>

      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={t.id === tab} className={t.id === tab ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="snippet">
        <CodeBlock language={current.lang} code={current.code.trimEnd()} title={current.file ?? current.lang} />
      </div>
      {tab === "continue" && (
        <p className="muted small">
          Store the key with Continue's secrets (<code>~/.continue/.env</code> line <code>FORGE_API_KEY=fk_...</code>). The full
          config with context providers lives in <code>integrations/vscode/config.yaml</code> in the repository.
        </p>
      )}
    </div>
  );
}
