"""Admin API: usage aggregation, models, status, conversations."""

from __future__ import annotations

import uuid

import pytest
from conftest import CSRF

CHAT = {"model": "forge-chat", "messages": [{"role": "user", "content": "hi"}]}


async def test_admin_routes_require_login(gateway):
    for path in ("/api/keys", "/api/usage", "/api/models", "/api/status", "/api/conversations", "/api/repos"):
        response = await gateway.client.get(path)
        assert response.status_code == 401, path


async def test_usage_aggregates_totals_keys_models_and_latency(gateway, bearer):
    key = await gateway.create_key("app", daily_token_quota=5000)
    for _ in range(2):
        assert (await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))).status_code == 200
    await gateway.client.post("/v1/embeddings", json={"model": "forge-embed", "input": ["a", "b"]}, headers=bearer(key))
    await gateway.client.post("/v1/chat/completions", json=CHAT | {"model": "nope"}, headers=bearer(key))

    usage = (await gateway.client.get("/api/usage", params={"range": "24h"})).json()
    assert usage["totals"] == {"requests": 4, "prompt_tokens": 2 * 11 + 6, "completion_tokens": 6, "errors": 1}
    assert usage["by_key"] == [
        {"key_id": key["id"], "name": "app", "requests": 4, "prompt_tokens": 28, "completion_tokens": 6, "errors": 1}
    ]
    by_model = {m["model"]: m for m in usage["by_model"]}
    assert by_model["forge-chat"]["requests"] == 2
    assert by_model["forge-embed"]["prompt_tokens"] == 6
    assert by_model["unknown"]["errors"] == 1  # unknown model names share one bucket
    assert by_model["forge-chat"]["p50_ms"] is not None

    assert len(usage["timeseries"]) in (24, 25)  # hourly buckets, both ends included
    assert sum(b["requests"] for b in usage["timeseries"]) == 4
    assert sum(b["tokens"] for b in usage["timeseries"]) == 34

    assert usage["latency"]["p50_ms"] is not None
    assert usage["latency"]["p95_ms"] >= usage["latency"]["p50_ms"]

    today = {k["name"]: k for k in usage["today_by_key"]}
    assert today["app"] == {"key_id": key["id"], "name": "app", "tokens_used": 34, "daily_token_quota": 5000}
    assert "web-console" in today


@pytest.mark.parametrize(("range_", "buckets"), [("7d", (7, 8)), ("30d", (30, 31))])
async def test_usage_daily_buckets(gateway, range_, buckets):
    await gateway.login()
    usage = (await gateway.client.get("/api/usage", params={"range": range_})).json()
    assert len(usage["timeseries"]) in buckets
    assert usage["totals"]["requests"] == 0


async def test_usage_rejects_unknown_range(gateway):
    await gateway.login()
    assert (await gateway.client.get("/api/usage", params={"range": "1y"})).status_code == 400


async def test_models_shows_pulled_and_loaded_state(gateway):
    gateway.ollama.loaded = [{"name": "qwen2.5-coder:7b", "size_vram": 5_100_000_000}]
    await gateway.login()
    body = (await gateway.client.get("/api/models")).json()
    assert body["backend"] == "ready"
    models = {m["alias"]: m for m in body["models"]}
    assert (models["forge-chat"]["pulled"], models["forge-chat"]["loaded"]) == (True, True)
    assert models["forge-chat"]["size_vram"] == 5_100_000_000
    assert (models["forge-autocomplete"]["pulled"], models["forge-autocomplete"]["loaded"]) == (False, False)
    assert models["forge-embed"]["pulled"] is True


async def test_models_does_not_wake_a_sleeping_backend(gateway):
    gateway.ollama.up = False
    gateway.state.backend.mark_down()
    await gateway.login()
    body = (await gateway.client.get("/api/models")).json()
    assert body["backend"] == "sleeping"
    assert all(m["pulled"] is None and m["loaded"] is False for m in body["models"])
    assert not gateway.ollama.requests


async def test_status_reports_queue_backend_and_gpu(gateway):
    await gateway.login()
    status = (await gateway.client.get("/api/status")).json()
    assert status["backend"] == "ready"
    assert status["queue"] == {"autocomplete": 0, "chat": 0, "batch": 0, "total": 0}
    assert status["inflight"] == 0
    assert status["max_inflight"] == 2
    assert status["gpu"]["name"] == "Fake GPU"


async def test_conversation_crud(gateway):
    await gateway.login()
    conversation_id = str(uuid.uuid4())
    payload = {
        "title": "Refactor parser",
        "model": "forge-chat",
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    }
    created = await gateway.client.put(f"/api/conversations/{conversation_id}", json=payload, headers=CSRF)
    assert created.status_code == 200
    assert created.json()["messages"] == payload["messages"]

    payload["messages"].append({"role": "user", "content": "more"})
    updated = await gateway.client.put(
        f"/api/conversations/{conversation_id}", json=payload | {"title": "Parser"}, headers=CSRF
    )
    assert updated.json()["title"] == "Parser"
    assert updated.json()["updated_at"] >= created.json()["updated_at"]

    listing = (await gateway.client.get("/api/conversations")).json()
    assert [(c["id"], c["title"]) for c in listing] == [(conversation_id, "Parser")]
    full = (await gateway.client.get(f"/api/conversations/{conversation_id}")).json()
    assert len(full["messages"]) == 3

    assert (await gateway.client.delete(f"/api/conversations/{conversation_id}", headers=CSRF)).status_code == 204
    assert (await gateway.client.get(f"/api/conversations/{conversation_id}")).status_code == 404


async def test_conversation_rejects_bad_messages(gateway):
    await gateway.login()
    response = await gateway.client.put(
        f"/api/conversations/{uuid.uuid4()}", json={"messages": [{"role": "robot", "content": "x"}]}, headers=CSRF
    )
    assert response.status_code == 400
