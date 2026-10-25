# Forge gateway

FastAPI service in front of Ollama. It provides an OpenAI-compatible `/v1` API, API keys and the
admin session, per-key rate limits and daily token quotas, one priority queue for the single GPU
shared by every replica, wake-up from zero replicas, usage metering, Prometheus metrics and the
admin API used by the web console. `docs/SPEC.md` section 2 is the contract.

```
request ─► auth (key or session+CSRF) ─► rpm / quota (Redis) ─► wake Ollama if asleep
        ─► GPU slot (Redis priority queue, Lua) ─► Ollama /v1/* ─► usage_events row + metrics
```

| Module | Role |
|---|---|
| `app/config.py` | `FORGE_*` settings, model list from `FORGE_MODELS_JSON` |
| `app/auth.py` | API keys (SHA-256 hashed), admin session cookie, CSRF header |
| `app/scheduler.py` | `GpuScheduler`: shared queue, leases with TTL, pub/sub, autocomplete supersession |
| `app/backend.py` | Ollama readiness, wake-up wait, `/internal/demand` for KEDA |
| `app/llm.py` | The single metered call path, used by `/v1` and by `app/rag` |
| `app/metering.py` | Rate limits, quotas, usage events, Prometheus metrics |
| `app/routes/` | `/v1`, `/api`, `/internal`, probes |
| `alembic/` | One migration creating every table (including the RAG ones) |

## Develop

```bash
uv sync                      # Python 3.12, exact pins from uv.lock
uv run ruff check .
docker run -d --name forge-test-pg -e POSTGRES_USER=forge -e POSTGRES_PASSWORD=forge-test \
  -e POSTGRES_DB=forge_test -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg17-trixie
uv run pytest                # set FORGE_TEST_DATABASE_URL to use another Postgres
```

Redis is faked with fakeredis (Lua through lupa) and Ollama with an in-process ASGI fake
(`tests/fake_ollama.py`), so the tests need no GPU.

## Run

```bash
python -m app.migrate                                  # needs only FORGE_DATABASE_URL
uvicorn --factory app.main:create_app --port 8000      # needs FORGE_ADMIN_PASSWORD, FORGE_SESSION_SECRET
```

The image (`Dockerfile`) runs as uid 10001 and works with a read-only root filesystem if `/tmp`
is a writable emptyDir. After changing dependencies, regenerate the hashed requirements used by
the image with `uv lock && uv export --no-dev --frozen --no-emit-project -o requirements.txt`.
`pyproject.toml` sets `exclude-newer`, so the resolver only picks releases from on or before
2026-10-11.
