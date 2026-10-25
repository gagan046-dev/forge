"""Scale-from-zero wake-up, the KEDA demand endpoint, probes and metrics."""

from __future__ import annotations

import asyncio

import pytest

CHAT = {"model": "forge-chat", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def settings_overrides():
    return {"wake_timeout_s": 3.0, "idle_seconds": 600}


async def test_demand_reports_idle_backend_as_zero(gateway):
    response = await gateway.client.get("/internal/demand")
    assert response.status_code == 200
    assert response.json() == {"demand": 0, "queued": 0, "inflight": 0, "waking": 0, "idle_for_s": None}


async def test_request_wakes_a_sleeping_backend(gateway, bearer):
    key = await gateway.create_key()
    gateway.ollama.up = False  # Ollama scaled to zero
    gateway.state.backend.mark_down()

    request = asyncio.create_task(gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)))

    # While it waits, the demand signal asks KEDA for a replica and the UI shows "waking".
    async def demand():
        return (await gateway.client.get("/internal/demand")).json()

    for _ in range(100):
        current = await demand()
        if current["waking"] >= 1:
            break
        await asyncio.sleep(0.02)
    assert current["waking"] == 1
    assert current["demand"] >= 2  # the waiter + recent activity
    assert not request.done()

    await gateway.login()
    status = (await gateway.client.get("/api/status")).json()
    assert status["backend"] == "waking"

    gateway.ollama.up = True  # the pod became ready
    response = await asyncio.wait_for(request, 5)
    assert response.status_code == 200
    assert (await demand())["waking"] == 0


async def test_wake_timeout_returns_503(gateway, bearer, database_url):
    key = await gateway.create_key()
    gateway.ollama.up = False
    gateway.state.backend.mark_down()
    response = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "backend_unavailable"
    assert int(response.headers["retry-after"]) > 0


async def test_demand_counts_inflight_queued_and_recent_activity(gateway, bearer):
    key = await gateway.create_key()
    await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    after_request = (await gateway.client.get("/internal/demand")).json()
    assert after_request["demand"] == 1  # nothing running, but active within IDLE_SECONDS
    assert after_request["idle_for_s"] is not None and after_request["idle_for_s"] < 5

    gateway.ollama.gate = asyncio.Event()  # hold the upstream call open
    calls = [
        asyncio.create_task(gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)))
        for _ in range(3)
    ]
    for _ in range(200):
        current = (await gateway.client.get("/internal/demand")).json()
        if current["inflight"] == 2 and current["queued"] == 1:
            break
        await asyncio.sleep(0.02)
    assert (current["inflight"], current["queued"], current["demand"]) == (2, 1, 4)
    gateway.ollama.gate.set()
    assert [r.status_code for r in await asyncio.gather(*calls)] == [200, 200, 200]


async def test_demand_drops_to_zero_after_idle_period(gateway, bearer):
    gateway.state.settings.idle_seconds = 0
    key = await gateway.create_key()
    await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert (await gateway.client.get("/internal/demand")).json()["demand"] == 0


async def test_probes_and_metrics(gateway, bearer):
    assert (await gateway.client.get("/healthz")).json() == {"status": "ok"}
    ready = await gateway.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "redis": "ok"}

    key = await gateway.create_key()
    await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    metrics = (await gateway.client.get("/metrics")).text
    for name in (
        "forge_requests_total",
        "forge_tokens_total",
        "forge_request_duration_seconds",
        "forge_ttft_seconds",
        "forge_queue_wait_seconds",
        "forge_queue_depth",
        "forge_inflight",
        "forge_backend_up",
        "forge_demand",
    ):
        assert name in metrics, name
    assert 'forge_requests_total{endpoint="chat",model="forge-chat",status="200"}' in metrics
