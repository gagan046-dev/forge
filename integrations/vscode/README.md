# Forge in VS Code (Continue)

[Continue](https://marketplace.visualstudio.com/items?itemName=Continue.continue) is an open-source
VS Code extension for chat, inline edit and tab autocomplete. `config.yaml` in this folder points all
of it at Forge, so your code never leaves the laptop.

| Continue feature | Role(s) | Forge model | Forge endpoint |
|---|---|---|---|
| Chat sidebar, inline edit (Ctrl+I), apply code block | `chat`, `edit`, `apply` | `forge-chat` (qwen2.5-coder:7b) | `POST /v1/chat/completions` |
| Tab autocomplete | `autocomplete` | `forge-autocomplete` (qwen2.5-coder:1.5b-base) | `POST /v1/completions` |
| `@codebase` / docs indexing | `embed` | `forge-embed` (nomic-embed-text:v1.5) | `POST /v1/embeddings` |

## Install

1. Forge is running (`scripts\up.ps1`) and Windows resolves `forge.localhost`. Browsers do this on
   their own, VS Code does not: `scripts\up.ps1` adds the hosts-file entries, or run
   `scripts\add-hosts.ps1` from an Administrator PowerShell. Check with
   `curl.exe http://forge.localhost:8082/v1/models` (a 401 means Forge is reachable).
2. Create an API key: open http://forge.localhost:8082, log in, go to **API keys**, **Create**, name it
   `vscode`. Copy the `fk_...` key now, it is shown only once.
3. Install the extension: `code --install-extension Continue.continue` (or search "Continue" in the
   Extensions view).
4. Copy the config and store the key next to it (PowerShell):
   ```powershell
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.continue" | Out-Null
   Copy-Item integrations\vscode\config.yaml "$env:USERPROFILE\.continue\config.yaml"
   # paste your key instead of fk_... ; this file stays on your machine, never commit it
   Set-Content -Encoding ascii "$env:USERPROFILE\.continue\.env" "FORGE_API_KEY=fk_..."
   ```
   If you already have a `config.yaml`, merge the three `models:` entries into it instead.
5. Reload VS Code (Command Palette, **Developer: Reload Window**). In the Continue sidebar choose the
   **Forge** config and the **Forge Chat** model.
6. Try it: ask something in chat; select code and press Ctrl+I; type in a code file and wait for the
   grey tab suggestion (Tab accepts it). Autocomplete requests show up on the **Usage** page as
   `completions` for `forge-autocomplete`.

The first request after Forge has been idle wakes the GPU backend (scale-from-zero), which can take a
minute. Continue waits for it.

## Why the config looks the way it does

**The API key comes from `.env`.** `apiKey: ${{ secrets.FORGE_API_KEY }}` makes Continue look the
value up in `~/.continue/.env`, then `<workspace>/.continue/.env`, then `<workspace>/.env`
([LocalPlatformClient.ts](https://github.com/continuedev/continue/blob/main/core/config/yaml/LocalPlatformClient.ts)),
so `config.yaml` itself holds no secret and can be shared.

**`provider: openai` + `apiBase`** is Continue's way to talk to any OpenAI-compatible server. Forge
speaks that API, so no Forge-specific plugin is needed. Continue appends a trailing `/` to `apiBase`
and resolves `chat/completions`, `completions` and `embeddings` against it.

**How autocomplete reaches the model (fill-in-the-middle).** Code completion needs the text *before*
the cursor (prefix) and *after* it (suffix). There are two ways to send both:

- Native FIM: send `prompt` = prefix and `suffix` = suffix to `/v1/completions` and let the server
  build the model's FIM prompt. Forge supports this (Ollama fills in the Qwen FIM template);
  `integrations/python/example.py fim` uses it.
- Client-side template: the client builds the FIM prompt string itself and sends it as a plain
  `prompt`.

Continue's `openai` provider uses the second way. Its `supportsFim()` is `false`, so Continue renders
the model's FIM template with the prefix and suffix and calls `streamComplete(..., { raw: true })`,
which the openai provider sends as a streaming `POST /v1/completions` with only `prompt`
([CompletionStreamer.ts](https://github.com/continuedev/continue/blob/main/core/autocomplete/generation/CompletionStreamer.ts),
[OpenAI.ts](https://github.com/continuedev/continue/blob/main/core/llm/llms/OpenAI.ts)). Two settings
follow from that:

- `autocompleteOptions.template` is set to Qwen2.5-Coder's FIM format
  `<|fim_prefix|>{{{prefix}}}<|fim_suffix|>{{{suffix}}}<|fim_middle|>`. Continue picks a template from
  the *model name*
  ([AutocompleteTemplate.ts](https://github.com/continuedev/continue/blob/main/core/autocomplete/templating/AutocompleteTemplate.ts)),
  and the alias `forge-autocomplete` does not contain "qwen", so without this it would fall back to a
  StarCoder-style template the model was not trained on. If `gitops/models.yaml` ever switches the
  autocomplete model to another family, change this template too.
- `defaultCompletionOptions.stop` lists Qwen's special tokens so a completion ends when the model
  tries to open a new file or FIM section. Continue adds these to its own stop words.
- `useLegacyCompletionsEndpoint: true` states explicitly that this model is served by
  `/v1/completions` (it is a base model, it has no chat template).

**The autocomplete model must be a base model.** Because Continue sends the raw FIM prompt with no
`suffix` field, Ollama runs it through the model's own prompt template. A base model's template
passes the text through unchanged; an instruct model's template wraps it as a chat message, and the
model then answers like a chat ("Certainly! Below is...", markdown fences, the whole file repeated)
instead of filling the gap. Measured with `qwen2.5-coder:7b` (instruct): 9 of 15 completions were
chat prose. So if you change `forge-autocomplete` in `gitops/models.yaml`, pick a `-base` tag (for
example `qwen2.5-coder:1.5b-base` or `qwen2.5-coder:7b-base`), never an instruct one.

Either way the request goes through Forge's autocomplete path: the `autocomplete` role gives it the
highest GPU-queue priority, and a newer keystroke's request supersedes the same key's older queued
one, so typing stays responsive while chat or embedding work is running.

**Roles.** Valid roles are `chat`, `autocomplete`, `embed`, `rerank`, `edit`, `apply`, `summarize`
(and `subagent`). A model without `roles` gets `chat, edit, apply, summarize`, so the roles are listed
explicitly to keep the 1.5B base model out of chat and the chat model out of autocomplete.

**Not enabled: tool use (agent mode).** Continue decides tool support from the model name and does
not know `forge-chat`. A 7B model is not reliable at multi-step tool calling, so the config leaves
`capabilities` unset and agent mode falls back to Continue's prompt-based tools. Add
`capabilities: [tool_use]` under Forge Chat to experiment.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `getaddrinfo ENOTFOUND forge.localhost` | hosts entries missing: run `scripts\add-hosts.ps1` as Administrator |
| `401 invalid_api_key` | `.env` missing or key revoked; check `%USERPROFILE%\.continue\.env` and the API keys page |
| `429 rate_limit_exceeded` / `quota_exceeded` | raise the key's limits on the API keys page |
| No autocomplete suggestions | Continue status bar item must not say "disabled"; check **Output > Continue** for errors |
| First answer takes a minute | the GPU backend was asleep; later requests are fast |

## References

- Continue `config.yaml` reference: https://docs.continue.dev/reference
- Continue autocomplete deep dive: https://docs.continue.dev/customize/deep-dives/autocomplete
- Continue config schema source (`@continuedev/config-yaml`, which this file was validated against):
  https://github.com/continuedev/continue/blob/main/packages/config-yaml/src/schemas/models.ts
- Qwen2.5-Coder FIM format: https://github.com/QwenLM/Qwen2.5-Coder#3-file-level-code-completion-fill-in-the-middle
