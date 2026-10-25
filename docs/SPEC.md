# Forge: build specification (the contract every component follows)

Forge is a private, OpenAI-compatible AI platform plus coding assistant running on one local NVIDIA RTX 4070 Laptop GPU (8 GB VRAM) inside a k3d Kubernetes cluster on Windows 11 + Docker Desktop (WSL2). It replaces paid APIs (OpenRouter) for the user's other projects and works as a private Copilot in VS Code.

Repository: `https://github.com/gagan046-dev/forge` (public). Images: `ghcr.io/gagan046-dev/forge-<name>:sha-<7 hex>`.

## 0. Global rules (all components)
- **No placeholders, stubs, TODOs or fake data.** Every feature listed here must really work.
- **Dependency versions:** exact pins, each released **at least 2 weeks before 2026-10-25**, i.e. on or before 2026-10-11. Check release dates (PyPI, npm, GitHub releases, Docker Hub) rather than guessing. Never use `latest` tags.
- Never print, log or commit secrets. Plain-text secrets live only in `forge/secrets/`, which is git-ignored.
- Containers: non-root, `readOnlyRootFilesystem` where practical, drop ALL capabilities, `seccompProfile: RuntimeDefault`, so Pod Security "restricted" applies.
- Code reads like a professional wrote it: clear names, short comments explaining *why*, no over-engineering.
- Windows host: PowerShell 5.1 scripts must work. Native commands' stderr is not an error; wrap them as in `C:\Users\gagan\Desktop\AIOPs\gpu-hello\scripts\up.ps1`, which uses a `Run` helper with `cmd /c "... 2>&1"` and checks `$LASTEXITCODE`.
- Shell scripts use LF line endings (`.gitattributes` has `* text=auto eol=lf`).

## 1. Architecture

```
VS Code (Continue) ─┐
Vigil / Pulse  ─────┼─► http://forge.localhost:8082/v1   (Traefik ingress, k3d LB port 8082→80)
Browser (web UI) ───┘        │
                             ▼
 namespace forge-prod / forge-dev:  forge-web (nginx, React SPA)  +  forge-gateway (FastAPI, 2 replicas prod / 1 dev)
                                    + postgres (pgvector image; keys, usage, conversations, RAG chunks)
                             │  OpenAI-compatible proxy, auth, quotas, priority GPU queue, metering, RAG
                             ▼
 namespace forge-inference:  ollama (Deployment, 1 replica, nvidia.com/gpu: 1, KEDA scales 0↔1)
                             redis (GPU scheduler queue + rate-limit counters, shared by dev and prod)
                             gpu-exporter (nvidia-smi → Prometheus metrics + JSON)
 namespace keda:            KEDA (ScaledObject on ollama, metrics-api trigger → gateway /internal/demand)
 namespace monitoring:      Prometheus + Grafana (dashboard "Forge")
 namespace argocd:          Argo CD (app-of-apps) · kube-system: sealed-secrets, nvidia-device-plugin
```

Hosts (all via `localhost:8082`): `forge.localhost` (prod), `forge-dev.localhost` (dev), `grafana.localhost`, `argocd.localhost`.

### Models: single source of truth `gitops/models.yaml`
```yaml
models:
  - alias: forge-chat            # what clients ask for
    name: qwen2.5-coder:7b       # Ollama model tag (pulled into the Ollama PVC)
    role: chat                   # chat | autocomplete | embedding
    contextLength: 8192
  - alias: forge-autocomplete
    name: qwen2.5-coder:1.5b-base
    role: autocomplete
    contextLength: 4096
  - alias: forge-embed
    name: nomic-embed-text:v1.5
    role: embedding
    dimensions: 768
```
The forge-inference chart and both forge env apps read it as a Helm value file (Argo CD multi-source `$values/gitops/models.yaml`; locally `helm template -f gitops/models.yaml`). Changing this file through a PR changes the deployed models: the Ollama pod's checksum annotation changes, so the pod restarts and its init container pulls the new models. The gateway gets the list as env `FORGE_MODELS_JSON` (JSON array of the objects above).

VRAM budget: about 6.8 GB free. 7B Q4 = 4.7 GB, 1.5B = 1.0 GB, embed = 0.3 GB, plus KV cache. Ollama env: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_MAX_LOADED_MODELS=3`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_CONTEXT_LENGTH=8192`.

## 2. Gateway (`gateway/`, Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, redis-py asyncio, httpx, prometheus-client, pydantic-settings)

Package layout (fixed, so the RAG module can plug in):
```
gateway/app/main.py          FastAPI app factory create_app(); mounts routers; lifespan (db, redis, httpx client)
gateway/app/config.py        Settings (pydantic-settings, env prefix FORGE_)
gateway/app/db.py            engine, async_session, Base (DeclarativeBase), get_session() dependency
gateway/app/models.py        ORM: ApiKey, UsageEvent, Conversation (and imports app.rag.models so Alembic sees them)
gateway/app/auth.py          require_principal (API key OR admin session), require_admin (session only)
gateway/app/scheduler.py     Priority enum + GpuScheduler (Redis-backed distributed priority queue)
gateway/app/backend.py       BackendManager: Ollama readiness, wake-up wait, demand tracking
gateway/app/llm.py           internal LLM client used by routes AND by RAG (see 2.4)
gateway/app/metering.py      token counting, quotas, rate limits, UsageEvent writes, Prometheus metrics
gateway/app/routes/openai.py /v1/*
gateway/app/routes/admin.py  /api/auth, /api/keys, /api/usage, /api/models, /api/status, /api/conversations
gateway/app/routes/internal.py /internal/demand, /healthz, /readyz, /metrics
gateway/app/rag/             (separate builder) models.py, indexer.py, retrieval.py, router.py → exports `router`
gateway/alembic/             migrations: ONE initial migration creating ALL tables in section 2.5, incl. RAG tables and `CREATE EXTENSION vector`
gateway/tests/               pytest + pytest-asyncio; fakes for Ollama (respx or an ASGI fake) and Redis (fakeredis)
gateway/Dockerfile           python:3.12-slim based, non-root uid 10001, uvicorn, runs `alembic upgrade head` via an entrypoint flag or a Helm pre-install/upgrade Job (chart decides; gateway provides `python -m app.migrate`)
```

### 2.1 Settings (env vars)
`FORGE_DATABASE_URL` (postgresql+asyncpg://...), `FORGE_REDIS_URL`, `FORGE_OLLAMA_URL` (http://ollama.forge-inference:11434), `FORGE_MODELS_JSON`, `FORGE_ENV` (dev|prod; prefixes Redis keys for rate limits only, NOT for the GPU queue, which is shared), `FORGE_ADMIN_PASSWORD`, `FORGE_SESSION_SECRET`, `FORGE_MAX_INFLIGHT` (default 2, GPU slots across all gateways), `FORGE_MAX_QUEUE` (default 32), `FORGE_QUEUE_TIMEOUT_S` (120), `FORGE_WAKE_TIMEOUT_S` (240), `FORGE_IDLE_SECONDS` (600), `FORGE_GPU_EXPORTER_URL` (http://gpu-exporter.forge-inference:9400), `FORGE_DEFAULT_RPM` (120), `FORGE_DEFAULT_DAILY_TOKENS` (2000000), `FORGE_RAG_ALLOWED_HOSTS` (github.com,gitlab.com,codeberg.org), `FORGE_LOG_LEVEL`.

### 2.2 Auth
- API key format: `fk_` + 40 chars from `secrets.token_urlsafe`. Stored as SHA-256 hex `key_hash`, plus `prefix` (first 10 chars) for display. The plain key is returned only once, at creation.
- `Authorization: Bearer <key>` on `/v1/*`. Revoked or unknown keys get 401.
- Admin session: `POST /api/auth/login {"password"}` compares with `FORGE_ADMIN_PASSWORD` (constant-time), then sets an HttpOnly, SameSite=Lax cookie `forge_session` (signed with itsdangerous using `FORGE_SESSION_SECRET`, 12 h). Login attempts are rate-limited (5 per minute per IP via Redis).
- `require_principal`: a Bearer key, OR an admin session cookie. A session is attributed to a built-in key row named `web-console` (created at startup if missing; it has no usable hash and can't be revoked). This lets the web UI call `/v1/*` with the cookie.
- CSRF: for cookie-authenticated state-changing requests, require header `X-Forge-Csrf: 1`. The SPA always sends it; cross-site forms cannot.

### 2.3 OpenAI-compatible API (`/v1`)
- `GET /v1/models` → `{"object":"list","data":[{"id":alias,"object":"model","owned_by":"forge","role":...}]}`. Raw Ollama names are also accepted in requests if they are in FORGE_MODELS_JSON.
- `POST /v1/chat/completions`: OpenAI body; `stream` true/false. Map the alias to the Ollama name, forward to Ollama `/v1/chat/completions`. When streaming, set `stream_options: {"include_usage": true}` upstream, pass SSE chunks through unchanged (with model rewritten back to the alias), and read the usage chunk for metering. Priority: CHAT.
- `POST /v1/completions`: supports `prompt`, `suffix` (fill-in-the-middle for autocomplete), `max_tokens`, `temperature`, `stop`, `stream`. Forward to Ollama `/v1/completions`. Priority: AUTOCOMPLETE if the model role is autocomplete, else CHAT. **Autocomplete supersession:** a newer autocomplete request from the same principal cancels that principal's still-queued older autocomplete request (it returns 499-style `{"error":{"type":"superseded"}}`). This keeps typing responsive.
- `POST /v1/embeddings`: `input` string or list. Priority: BATCH. Forward to Ollama `/v1/embeddings`.
- Response headers: `x-request-id`, `x-forge-queue-ms`, `x-forge-model` (Ollama name).
- Errors are OpenAI-shaped: `{"error":{"message":..., "type":..., "code":...}}`. Status codes:
  - 400 `invalid_request_error` (unknown model, bad body);
  - 401 `invalid_api_key`;
  - 429 `rate_limit_exceeded` | `quota_exceeded` | `queue_full`, each with a `Retry-After` header;
  - 503 `backend_unavailable` (wake timeout) with `Retry-After`;
  - 504 `queue_timeout`.

### 2.4 GPU scheduler, backend wake-up, LLM client
- `Priority` IntEnum: `AUTOCOMPLETE=0, CHAT=1, BATCH=2` (lower runs first; FIFO within a priority).
- `GpuScheduler` (Redis, shared by every gateway replica in both envs; key prefix `forge:gpu:`):
  - each request enqueues a ticket in sorted set `forge:gpu:queue` with score `priority * 10**13 + enqueue_ms`;
  - it may start when it is among the first N tickets, where N = `MAX_INFLIGHT` − current leases;
  - acquiring is atomic (Lua script). A lease is a key with a TTL that the holder refreshes every 5 s, so a crashed replica's slots free themselves;
  - waiters use Redis pub/sub notifications plus a short poll fallback;
  - `MAX_QUEUE` full → 429 `queue_full`; waiting longer than `QUEUE_TIMEOUT_S` → 504;
  - exposes `queue_depth()`, `inflight()`, and per-priority depth.
  - Usage: `async with scheduler.slot(priority, principal_id, kind) as s: ...`, with cancellation support (client disconnect removes the ticket).
- `BackendManager`:
  - tracks Ollama readiness (`GET /api/version`, cached for 2 s);
  - `await ensure_ready()`: if Ollama is down, raise demand and wait until it is ready or `WAKE_TIMEOUT_S` passes;
  - Redis `forge:gpu:last_activity` holds the timestamp of the last request;
  - demand = queued + inflight + waking-waiters + (1 if `now − last_activity < IDLE_SECONDS`).
  - `GET /internal/demand` → `{"demand": <int>, "queued":..., "inflight":..., "idle_for_s":...}`, used by the KEDA metrics-api trigger. Both env gateways see the same Redis, so either answers for the whole GPU.
- `app/llm.py` (internal API the RAG module uses; signatures fixed):
  ```python
  class LLM:
      async def embed(self, texts: list[str], *, principal: Principal, priority: Priority = Priority.BATCH) -> list[list[float]]
      def stream_chat(self, messages: list[dict], *, model_alias: str, principal: Principal, priority: Priority = Priority.CHAT, max_tokens: int | None = None, temperature: float = 0.2) -> AsyncIterator[str]   # yields text deltas
      def alias_for_role(self, role: str) -> str      # "chat" -> "forge-chat"
  def get_llm(request: Request) -> LLM               # FastAPI dependency
  ```
  `Principal` (dataclass in `app/auth.py`): `key_id: int, name: str, is_admin: bool`. Both methods go through the scheduler, wake-up and metering, exactly like `/v1` requests.

### 2.5 Database schema (Postgres 17 + pgvector; all created by the single Alembic migration)
- `api_keys`: id serial PK, name text unique not null, prefix text, key_hash text unique null (null for web-console), rpm_limit int not null, daily_token_quota bigint not null, created_at timestamptz default now(), revoked_at timestamptz null, last_used_at timestamptz null, builtin bool default false.
- `usage_events`: id bigserial PK, ts timestamptz default now() (indexed), key_id int FK→api_keys, endpoint text (chat|completions|embeddings|rag), model text (alias), status int, prompt_tokens int, completion_tokens int, queue_ms int, ttft_ms int null, duration_ms int, tokens_per_s real null, error_type text null.
- `conversations`: id uuid PK, title text, model text, messages jsonb (list of {role, content}), created_at, updated_at.
- `repos`: id serial PK, url text unique, branch text, status text (queued|cloning|embedding|ready|failed), error text null, files_indexed int default 0, chunks int default 0, commit_sha text null, created_at, updated_at.
- `repo_chunks`: id bigserial PK, repo_id int FK→repos ON DELETE CASCADE, path text, start_line int, end_line int, content text, embedding vector(768), plus an HNSW index on `embedding vector_cosine_ops`.

### 2.6 Metering, quotas, metrics
- Rate limit: per key, `rpm_limit` requests per minute (Redis fixed window `forge:{env}:rpm:{key_id}:{minute}`).
- Daily token quota: Redis `forge:{env}:tok:{key_id}:{YYYYMMDD}`, checked before a request and incremented after it with prompt + completion tokens.
- Every request writes one `UsageEvent` (also failures). tokens_per_s = completion_tokens / (duration − ttft).
- Prometheus (`/metrics`):
  - `forge_requests_total{endpoint,model,status}`;
  - `forge_tokens_total{model,kind=prompt|completion}`;
  - `forge_request_duration_seconds{endpoint,model}` histogram;
  - `forge_ttft_seconds{model}` histogram;
  - `forge_queue_wait_seconds{priority}` histogram;
  - `forge_queue_depth{priority}` gauge;
  - `forge_inflight` gauge;
  - `forge_backend_up` gauge;
  - `forge_demand` gauge.

### 2.7 Admin API (`/api`, admin session + CSRF header)
- `GET /api/auth/me` → `{"authenticated": bool}`.
- `GET /api/keys` → list (no hashes). `POST /api/keys {name, rpm_limit?, daily_token_quota?}` → includes `key` once. `PATCH /api/keys/{id}` updates limits. `DELETE /api/keys/{id}` revokes.
- `GET /api/usage?range=24h|7d|30d` returns:
  - `totals {requests, prompt_tokens, completion_tokens, errors}`;
  - `by_key [...]` and `by_model [...]`;
  - `timeseries [{t, requests, tokens}]` (hourly buckets for 24h, daily otherwise);
  - `latency {p50_ms, p95_ms, ttft_p50_ms, ttft_p95_ms, tokens_per_s_avg}`;
  - `today_by_key` (tokens used vs quota).
- `GET /api/models` → each configured model: alias, name, role, `pulled` (in Ollama `/api/tags`), `loaded` (in `/api/ps`) with size_vram; plus `backend: "ready"|"sleeping"|"waking"`.
- `GET /api/status` → backend state, queue depth per priority, inflight, demand, idle_for_s, gpu (proxied from gpu-exporter `/gpu` JSON, or null if unreachable).
- `GET /api/conversations` (id, title, updated_at), `GET/PUT/DELETE /api/conversations/{id}` (PUT upserts {title, model, messages}).

### 2.8 RAG "Ask my repo" (`gateway/app/rag/`, router mounted at `/api/repos`, admin session)
- `POST /api/repos {"url": "https://github.com/owner/name", "branch": null}` creates a repo row (status queued) and starts background indexing (asyncio task; at most 1 index job at a time per gateway, coordinated with a Redis lock `forge:rag:lock`). Only `https://` URLs whose host is in `FORGE_RAG_ALLOWED_HOSTS`; no credentials in the URL.
- Indexing:
  1. `git clone --depth 1 [--branch]` into a temp dir (timeout 120 s), and record the commit sha;
  2. walk the files, skipping `.git`, `node_modules`, `vendor`, `dist`, `build`, `.venv`, lock files, binaries (NUL byte check) and files over 200 KB. Text/code extensions only; at most 3000 files and 30 MB total;
  3. chunk by lines (60 lines, 10 overlap) with a `# path` header;
  4. embed in batches of 32 through `LLM.embed` (BATCH priority);
  5. insert into `repo_chunks`. Status goes `cloning → embedding → ready` (or `failed` with the error). Re-indexing (`POST /api/repos/{id}/reindex`) replaces the chunks.
- `GET /api/repos` and `GET /api/repos/{id}`: status, counts, progress (`files_indexed`, `chunks`). `DELETE /api/repos/{id}`.
- `POST /api/repos/{id}/ask {"question", "top_k": 8}` returns SSE (`text/event-stream`):
  - first `event: sources` with data `[{"n":1,"path":..,"start_line":..,"end_line":..,"score":..}]`;
  - then `event: token` with data `{"t":"..."}`, repeated;
  - then `event: done` with data `{"usage":{...}}`;
  - on failure, `event: error` with data `{"message":..}`.
  The prompt instructs the model to answer only from the sources and cite them as [n]. Retrieval uses cosine distance through pgvector (`<=>`).

## 3. Web UI (`web/`, React 18 + TypeScript + Vite, react-router; served by nginx-unprivileged on 8080)
- Same origin as the gateway: ingress routes `/v1`, `/api`, `/internal` (blocked at ingress), `/metrics` (blocked) to the gateway, and `/` to web.
- Every fetch sends `credentials: 'same-origin'` and `X-Forge-Csrf: 1`.
- Pages:
  1. **Login** (admin password).
  2. **Chat**: streaming via `/v1/chat/completions` (parse SSE); model picker (aliases from `/v1/models`, chat role); presets (Explain code, Refactor, Write tests, Review, Free chat → system prompts); stop button (AbortController); markdown rendering with highlighted code blocks and a copy button; conversation history sidebar saved through `/api/conversations`; shows tokens/s and time to first token per answer.
  3. **Ask repo**: add a repo URL, see indexing status (polling), ask a question → streamed answer with clickable source citations [n] showing path:lines, with links to the file on the forge host (github blob URL).
  4. **API keys**: list, create (show the key once with a copy button and a warning), revoke, edit limits.
  5. **Usage**: range switch, totals, requests/tokens over time, per-key and per-model tables, latency p50/p95, TTFT, tokens/s, today's quota bars.
  6. **System**: backend state (sleeping/waking/ready), models (pulled/loaded/VRAM), queue depth and inflight (polling every 2 s), GPU utilisation/VRAM/temperature/power.
  7. **Connect**: copy-paste snippets for the OpenAI Python SDK, curl, and a VS Code Continue config.
- Design: must NOT look AI-generated or template-made. Dense, calm "developer console" look: neutral greys with one accent colour, system UI font plus a monospace font for numbers and code, 1px borders, no gradients, no glassmorphism, no emoji, no hero sections, no rounded-everything cards. Light and dark theme following the OS. Keyboard: Enter sends, Shift+Enter adds a newline, Esc stops.
- Tests: vitest for the SSE parser and API client; `npm run build` must pass with no type errors.

## 4. GPU exporter (`gpu-exporter/`, Python stdlib only)
Every 5 s it runs `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits`. It serves `:9400/metrics` (`forge_gpu_utilization_percent`, `forge_gpu_memory_used_bytes`, `forge_gpu_memory_total_bytes`, `forge_gpu_temperature_celsius`, `forge_gpu_power_watts`, `forge_gpu_up`) and `:9400/gpu` (JSON). It runs with `runtimeClassName: nvidia` and **no** `nvidia.com/gpu` request, so it doesn't take the only GPU. On WSL2 the runtime injects `/dev/dxg` and the libraries even without a request; this was verified in gpu-hello. It needs a base image where the injected nvidia-smi works (e.g. `ubuntu:24.04` / `python:3.12-slim`).

## 5. Cluster, charts, GitOps (`cluster/`, `charts/`, `gitops/`)
- `cluster/`: GPU node image copied and adapted from `C:\Users\gagan\Desktop\AIOPs\gpu-hello\k3d\`. Read that Dockerfile, the 2 entrypoint scripts and `docs/LEARNING-GUIDE.md` section 6 first; they encode 3 hard-won fixes.
  - Image `forge/k3s-gpu:v1.35.9-k3s1`.
  - `cluster/k3d-config.yaml`: cluster `forge`, 1 server, 0 agents, `gpuRequest: all`, API `127.0.0.1:6448`, port `8082:80@loadbalancer`, keep Traefik, disable metrics-server only if unused.
- `charts/forge-inference/`:
  - Ollama Deployment (Recreate strategy, `runtimeClassName: nvidia`, `nvidia.com/gpu: 1`, PVC 40Gi local-path, an init container that pulls every model in `.Values.models` and is idempotent, checksum annotation of the models list, env from section 1, readiness `/api/version`);
  - Service `ollama`;
  - KEDA `ScaledObject`: minReplicaCount 0, maxReplicaCount 1, pollingInterval 5, cooldownPeriod 60; triggers `metrics-api` on `http://forge-gateway.forge-prod.svc:8000/internal/demand` and the same for forge-dev, `valueLocation: demand`, `targetValue: "1"`, `activationTargetValue: "0"`. If one env is missing, KEDA must not fail: use `fallback` or document it, and test it;
  - Redis StatefulSet (small, with password from a secret; persistence off is fine);
  - gpu-exporter Deployment and Service;
  - NetworkPolicies: only gateways from forge-dev/forge-prod may reach ollama:11434 and redis:6379; Prometheus may scrape the exporter;
  - Grafana dashboard ConfigMap (label `grafana_dashboard: "1"`) with GPU and Forge panels.
- `charts/forge/` (one env per release):
  - gateway Deployment (replicas from values), migration Job (Helm hook pre-install/pre-upgrade, or an Argo CD PreSync hook), Service `forge-gateway:8000`;
  - web Deployment and Service;
  - Postgres StatefulSet (`pgvector/pgvector:pg17` pinned), PVC 5Gi;
  - Ingress (Traefik) host from values: `/` → web, `/v1` and `/api` → gateway; `/internal` and `/metrics` not exposed;
  - NetworkPolicies, PodDisruptionBudget for the gateway in prod, ServiceMonitor-free Prometheus scrape annotations (`prometheus.io/scrape`);
  - resources and probes everywhere; secrets referenced from the SealedSecret-created Secret `forge-secrets` (keys: `admin-password`, `session-secret`, `postgres-password`) plus `forge-redis` (key `redis-password`).
  - Values: `values.yaml`, `values-dev.yaml`, `values-prod.yaml`, `values-dev-images.yaml`, `values-prod-images.yaml` (CI bumps the image tags there).
- `gitops/`:
  - `bootstrap/argocd-values.yaml` (Argo CD Helm chart, ingress `argocd.localhost`, insecure server behind Traefik) and `bootstrap/root-app.yaml` (app-of-apps → `gitops/apps/`);
  - `apps/` Argo CD Applications: sealed-secrets, keda, kube-prometheus-stack OR prometheus + grafana (pick the lightest that gives Prometheus + Grafana with sidecar dashboards; RAM matters), forge-inference, forge-dev, forge-prod. Use multi-source for `$values/gitops/models.yaml`. Sync waves order CRDs (keda, sealed-secrets) before users;
  - `envs/dev/`, `envs/prod/`: SealedSecret manifests (generated by `scripts/seal-secrets.ps1`), namespaces with Pod Security labels.
  - The NVIDIA device plugin DaemonSet (from gpu-hello `k8s/10-device-plugin.yaml`) is applied by `scripts/up.ps1` before Argo CD, or as an Argo app in wave -1.

## 6. CI/CD, scripts, integrations (`.github/`, `scripts/`, `integrations/`, `bench/`)
- `.github/workflows/ci.yaml`: on PR and push to main. Jobs:
  1. gateway lint (ruff) + pytest (Postgres pgvector service container + Redis service);
  2. web typecheck + vitest + build;
  3. helm lint + `helm template` for both charts with all value files;
  4. on main only, build and push 3 images (`forge-gateway`, `forge-web`, `forge-gpu-exporter`) to GHCR with tag `sha-<7>`, then commit the tag bump to `charts/forge/values-dev-images.yaml` and `charts/forge-inference/values-images.yaml` with `[skip ci]`.
  Actions pinned by full commit SHA; least-privilege `permissions`; concurrency groups. Follow the patterns in `C:\Users\gagan\Desktop\AIOPs\vigil\.github\workflows\` (read them).
- `.github/workflows/promote.yaml`: workflow_dispatch(tag) → PR copying the dev image tags to `values-prod-images.yaml`.
- `scripts/up.ps1`: does everything from zero, idempotently:
  1. prerequisites;
  2. build the node image;
  3. create the cluster;
  4. node DNS fix and device plugin;
  5. wait for `nvidia.com/gpu`;
  6. install Argo CD (Helm) and the root app;
  7. wait for the sealed-secrets controller, then run `seal-secrets.ps1` if `gitops/envs/*/forge-secrets.sealed.yaml` don't exist or the controller key changed (explain re-sealing);
  8. wait for all apps Healthy;
  9. print URLs and where the admin password is stored.
  Also `scripts/down.ps1` and `scripts/seal-secrets.ps1` (generates random secrets into `forge/secrets/*.txt` (git-ignored), seals them with kubeseal, never prints them). kubeseal: download a pinned release to `C:\Tools\kubeseal\` if it's missing.
- `integrations/vscode/config.yaml`: a Continue config using Forge for chat (`forge-chat`), edit, autocomplete (`forge-autocomplete`, FIM through `/v1/completions` + `suffix`) and embeddings (`forge-embed`). Research Continue's current config.yaml schema and how its `openai` provider does autocomplete/FIM, and make it correct. `integrations/vscode/README.md` gives install steps.
- `integrations/python/example.py`: OpenAI SDK pointed at Forge (chat, streaming, embeddings).
- `bench/loadtest.py`: async load test (httpx) that mixes autocomplete/chat/embeddings at configurable concurrency and reports p50/p95 latency, TTFT, tokens/s, 429 count, and priority behaviour (autocomplete p95 while chat floods). `bench/bench.py`: per-model tokens/s and VRAM report into `results/`.

## 7. Definition of done
Everything above deployed by `scripts/up.ps1` on the real GPU cluster, and each feature in the README demo checklist verified with real requests: chat streaming, FIM completion, embeddings, key create/revoke/limits, 429s, priority queue, usage numbers, RAG on a real public repo with citations, scale-to-zero and wake-up with VRAM freed, model switch via models.yaml PR, CI green with images on GHCR, Argo CD dev and prod Healthy, Grafana dashboard with live data.
