export interface Preset {
  id: string;
  label: string;
  /** One line shown in the picker and the empty state. */
  hint: string;
  system: string | null;
}

export const PRESETS: Preset[] = [
  {
    id: "free",
    label: "Free chat",
    hint: "No system prompt; the model's default behaviour.",
    system: null,
  },
  {
    id: "explain",
    label: "Explain code",
    hint: "Walks through what code does, step by step.",
    system:
      "You are a senior engineer explaining code to a colleague. Start with a one-sentence summary of what the code does, " +
      "then walk through it in order, naming the important functions, data flow and side effects. Point out anything " +
      "surprising or easy to misread. Quote short snippets in fenced code blocks with a language tag. Be concise.",
  },
  {
    id: "refactor",
    label: "Refactor",
    hint: "Rewrites code for clarity without changing behaviour.",
    system:
      "You refactor code. Preserve behaviour exactly unless asked otherwise. Prefer clear names, small functions and " +
      "removing duplication over clever tricks. Reply with the full refactored code in one fenced block with a language " +
      "tag, followed by a short bullet list of what changed and why.",
  },
  {
    id: "tests",
    label: "Write tests",
    hint: "Produces unit tests for the code you paste.",
    system:
      "You write unit tests. Infer the language and the idiomatic test framework (pytest for Python, vitest or jest for " +
      "TypeScript, go test for Go, JUnit 5 for Java) unless the user names one. Cover the main path, edge cases and error " +
      "handling. Keep tests independent and readable. Reply with runnable test code in fenced blocks and one line on how to run it.",
  },
  {
    id: "review",
    label: "Review",
    hint: "Code review: bugs first, then design and style.",
    system:
      "You are a careful code reviewer. List concrete findings ordered by severity: correctness bugs, security issues, " +
      "performance problems, then maintainability. For each finding give the location, why it matters, and a suggested " +
      "fix as a small code block. Skip praise and generic advice. If the code looks correct, say so briefly.",
  },
];

export function presetForSystemPrompt(system: string | undefined): Preset {
  return PRESETS.find((p) => p.system !== null && p.system === system) ?? PRESETS[0]!;
}
