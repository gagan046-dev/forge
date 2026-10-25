# forge-web

The Forge console: a React 18 + TypeScript single-page app served by nginx on port 8080.
In the cluster it shares an origin with the gateway: the ingress sends `/v1` and `/api` to
`forge-gateway:8000` and everything else here.

| Page | Route | Backed by |
|---|---|---|
| Login | `/login` | `POST /api/auth/login`, `GET /api/auth/me` |
| Chat | `/chat`, `/chat/:id` | `GET /v1/models`, streaming `POST /v1/chat/completions`, `/api/conversations` |
| Ask repo | `/repos` | `/api/repos` (polled while indexing), SSE `POST /api/repos/{id}/ask` |
| API keys | `/keys` | `/api/keys` |
| Usage | `/usage` | `GET /api/usage?range=24h\|7d\|30d` (refreshed every 30 s) |
| System | `/system` | `GET /api/status` (every 2 s), `GET /api/models` (every 5 s) |
| Connect | `/connect` | `GET /v1/models` for the model names in the snippets |

## How it talks to the gateway

```
browser ──fetch(credentials: same-origin, X-Forge-Csrf: 1)──► /api/*, /v1/*  (gateway)
        ◄── forge_session cookie (HttpOnly, set by /api/auth/login)
```

- `src/api/client.ts` is the only place that calls `fetch`. It adds the CSRF header, turns
  every non-2xx response into an `ApiError` (`status`, OpenAI-style `type`, `retryAfter`)
  and sends the user to the login page when an `/api` call returns 401.
- `src/api/sse.ts` is an incremental Server-Sent Events parser. Network chunks can split a
  line, a field, a CRLF pair or a multi-byte UTF-8 character anywhere; the parser keeps the
  unfinished tail and only acts on complete lines.
- `src/api/stream.ts` builds the two streams on top of it: OpenAI chat chunks (text deltas,
  `finish_reason`, the final `usage` chunk, `[DONE]`) and the RAG events
  (`sources`, `token`, `done`, `error`). Stop buttons abort the request with an `AbortController`.
- `src/api/normalize.ts` reads admin responses whose row field names the spec leaves open
  (usage breakdowns, GPU stats) into typed objects, so field-name changes are fixed in one place.

## Local development

```bash
npm ci
npm run dev        # http://localhost:5173, proxies /v1 and /api to http://localhost:8000
npm run typecheck
npm run test       # vitest: SSE parser, API client, streams, normalizers, helpers
npm run build      # type-checks, then writes dist/
```

Run the gateway locally on port 8000 (or `kubectl -n forge-dev port-forward svc/forge-gateway 8000:8000`)
and log in with the admin password.

## Container

```bash
docker build -t forge-web web/
docker run --rm -p 8080:8080 --read-only --tmpfs /tmp forge-web
```

- Multi-stage: `node:24.21.0-alpine3.24` builds, `nginxinc/nginx-unprivileged:1.31.5-alpine3.24`
  serves (uid 101, port 8080), both pinned by digest.
- `nginx/default.conf`: SPA fallback to `index.html`, hashed `/assets/*` cached for a year
  (`immutable`), `index.html` sent with `no-cache` so a new deployment loads on the next visit,
  `/healthz` for probes, and a JSON 404 for `/api`, `/v1`, `/internal`, `/metrics` if the
  ingress ever routes them here by mistake.
- With `readOnlyRootFilesystem: true`, mount an `emptyDir` at `/tmp`; nginx keeps its pid
  file and temp buffers there.

## Design notes

One hand-written stylesheet (`src/styles/global.css`): CSS variables for every colour, a
second token block for dark mode via `prefers-color-scheme`, system UI font plus a monospace
for numbers, code and keys, 1px borders, 4 to 6px radii, one accent colour (forge orange)
and separate status colours for ready / warning / error. Charts use uPlot (canvas, about
45 KB) with one measure per chart.
