"""Per-key rate limits and daily token quotas."""

from __future__ import annotations

from conftest import CSRF, usage_events

CHAT = {"model": "forge-chat", "messages": [{"role": "user", "content": "hi"}]}


async def test_rpm_limit_returns_429_with_retry_after(gateway, bearer, database_url):
    key = await gateway.create_key("limited", rpm_limit=2)
    statuses = []
    for _ in range(3):
        response = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
        statuses.append(response.status_code)
    assert statuses == [200, 200, 429]
    error = response.json()["error"]
    assert error["type"] == "rate_limit_exceeded"
    assert 1 <= int(response.headers["retry-after"]) <= 60
    # The refusal is recorded too.
    assert [(e["status"], e["error_type"]) for e in await usage_events(database_url)][-1] == (
        429,
        "rate_limit_exceeded",
    )


async def test_rate_limits_are_per_key(gateway, bearer):
    a = await gateway.create_key("a", rpm_limit=1)
    b = await gateway.create_key("b", rpm_limit=1)
    assert (await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(a))).status_code == 200
    assert (await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(a))).status_code == 429
    assert (await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(b))).status_code == 200


async def test_daily_quota_returns_429_once_used_up(gateway, bearer):
    # The fake reports 14 tokens per chat call, so the second call finds the quota exhausted.
    key = await gateway.create_key("small", daily_token_quota=10)
    first = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert first.status_code == 200
    second = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "quota_exceeded"
    assert int(second.headers["retry-after"]) > 0

    # Raising the quota takes effect immediately.
    await gateway.client.patch(f"/api/keys/{key['id']}", json={"daily_token_quota": 1000}, headers=CSRF)
    third = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert third.status_code == 200


async def test_rate_limit_keys_are_prefixed_by_env(gateway, bearer):
    key = await gateway.create_key()
    await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    keys = [k async for k in gateway.redis.scan_iter("forge:dev:*")]
    assert any(k.startswith(f"forge:dev:rpm:{key['id']}:") for k in keys)
    assert any(k.startswith(f"forge:dev:tok:{key['id']}:") for k in keys)


async def test_queue_full_maps_to_429(gateway, bearer):
    key = await gateway.create_key()
    scheduler = gateway.state.scheduler
    scheduler.max_queue = 0  # nothing may queue
    response = await gateway.client.post(
        "/v1/embeddings", json={"model": "forge-embed", "input": "x"}, headers=bearer(key)
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "queue_full"
    assert response.headers["retry-after"]
