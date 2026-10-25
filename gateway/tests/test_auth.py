"""API keys, revocation, the admin session cookie and CSRF."""

from __future__ import annotations

import hashlib

import asyncpg
from conftest import ADMIN_PASSWORD, CSRF, TEST_DB, _asyncpg_dsn

from app.auth import generate_api_key


def test_generated_keys_have_the_documented_format():
    key = generate_api_key()
    assert key.startswith("fk_")
    assert len(key) == 43  # "fk_" + 40
    assert generate_api_key() != key


async def test_key_is_shown_once_and_stored_hashed(gateway, database_url):
    created = await gateway.create_key("ci", rpm_limit=10, daily_token_quota=1000)
    assert created["key"].startswith("fk_")
    assert created["prefix"] == created["key"][:10]
    assert (created["rpm_limit"], created["daily_token_quota"]) == (10, 1000)

    listing = (await gateway.client.get("/api/keys")).json()
    assert all("key" not in k and "key_hash" not in k for k in listing)
    assert {k["name"] for k in listing} == {"web-console", "ci"}

    conn = await asyncpg.connect(_asyncpg_dsn(database_url, TEST_DB))
    try:
        stored = await conn.fetchval("SELECT key_hash FROM api_keys WHERE name = 'ci'")
    finally:
        await conn.close()
    assert stored == hashlib.sha256(created["key"].encode()).hexdigest()


async def test_bearer_key_authenticates_and_unknown_key_is_401(gateway, bearer):
    key = await gateway.create_key()
    gateway.client.cookies.clear()

    ok = await gateway.client.get("/v1/models", headers=bearer(key))
    assert ok.status_code == 200
    assert {m["id"] for m in ok.json()["data"]} == {"forge-chat", "forge-autocomplete", "forge-embed"}

    bad = await gateway.client.get("/v1/models", headers={"Authorization": "Bearer fk_not-a-real-key"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_api_key"

    missing = await gateway.client.get("/v1/models")
    assert missing.status_code == 401
    assert missing.json()["error"]["type"] == "invalid_api_key"


async def test_revoked_key_is_rejected(gateway, bearer):
    key = await gateway.create_key()
    revoked = await gateway.client.delete(f"/api/keys/{key['id']}", headers=CSRF)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    gateway.client.cookies.clear()
    response = await gateway.client.get("/v1/models", headers=bearer(key))
    assert response.status_code == 401
    assert "revoked" in response.json()["error"]["message"]


async def test_web_console_key_cannot_be_revoked(gateway):
    await gateway.login()
    keys = (await gateway.client.get("/api/keys")).json()
    console = next(k for k in keys if k["name"] == "web-console")
    assert console["builtin"] is True
    response = await gateway.client.delete(f"/api/keys/{console['id']}", headers=CSRF)
    assert response.status_code == 400


async def test_patch_updates_limits_and_duplicate_names_conflict(gateway):
    key = await gateway.create_key("dup")
    patched = await gateway.client.patch(f"/api/keys/{key['id']}", json={"rpm_limit": 7}, headers=CSRF)
    assert patched.status_code == 200
    assert patched.json()["rpm_limit"] == 7
    again = await gateway.client.post("/api/keys", json={"name": "dup"}, headers=CSRF)
    assert again.status_code == 409


async def test_login_sets_an_httponly_session_cookie(gateway):
    me = await gateway.client.get("/api/auth/me")
    assert me.json() == {"authenticated": False}

    wrong = await gateway.client.post("/api/auth/login", json={"password": "nope-nope"}, headers=CSRF)
    assert wrong.status_code == 401

    response = await gateway.client.post("/api/auth/login", json={"password": ADMIN_PASSWORD}, headers=CSRF)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("forge_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "SameSite=Lax" in cookie
    assert (await gateway.client.get("/api/auth/me")).json() == {"authenticated": True}

    await gateway.client.post("/api/auth/logout", headers=CSRF)
    gateway.client.cookies.clear()
    assert (await gateway.client.get("/api/auth/me")).json() == {"authenticated": False}


async def test_tampered_session_cookie_is_rejected(gateway):
    gateway.client.cookies.set("forge_session", "eyJzdWIiOiJhZG1pbiJ9.forged.signature")
    response = await gateway.client.get("/api/keys")
    assert response.status_code == 401


async def test_admin_api_requires_the_session_not_a_key(gateway, bearer):
    key = await gateway.create_key()
    gateway.client.cookies.clear()
    response = await gateway.client.get("/api/keys", headers=bearer(key))
    assert response.status_code == 401


async def test_cookie_writes_need_the_csrf_header(gateway):
    await gateway.login()
    blocked = await gateway.client.post("/api/keys", json={"name": "x"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["type"] == "csrf_failed"
    # Reads don't need it.
    assert (await gateway.client.get("/api/keys")).status_code == 200
    # Login itself requires it too (prevents login CSRF).
    gateway.client.cookies.clear()
    assert (await gateway.client.post("/api/auth/login", json={"password": ADMIN_PASSWORD})).status_code == 403


async def test_session_can_call_v1_and_is_attributed_to_web_console(gateway):
    await gateway.login()
    no_csrf = await gateway.client.post(
        "/v1/chat/completions", json={"model": "forge-chat", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert no_csrf.status_code == 403

    response = await gateway.client.post(
        "/v1/chat/completions",
        json={"model": "forge-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers=CSRF,
    )
    assert response.status_code == 200
    usage = (await gateway.client.get("/api/usage?range=24h")).json()
    assert [k["name"] for k in usage["by_key"]] == ["web-console"]


async def test_login_attempts_are_rate_limited(gateway):
    statuses = []
    for _ in range(7):
        r = await gateway.client.post("/api/auth/login", json={"password": "wrong-password"}, headers=CSRF)
        statuses.append(r.status_code)
    assert statuses[:5] == [401] * 5
    assert statuses[5:] == [429, 429]
    assert "retry-after" in r.headers


async def test_successful_logins_do_not_use_up_the_failure_limit(gateway):
    # Behind NAT every client can share one address; successes must not lock the admin out.
    for _ in range(8):
        ok = await gateway.client.post("/api/auth/login", json={"password": ADMIN_PASSWORD}, headers=CSRF)
        assert ok.status_code == 200
    for _ in range(5):
        wrong = await gateway.client.post("/api/auth/login", json={"password": "wrong-password"}, headers=CSRF)
        assert wrong.status_code == 401
    blocked = await gateway.client.post("/api/auth/login", json={"password": ADMIN_PASSWORD}, headers=CSRF)
    assert blocked.status_code == 429


async def test_logout_revokes_the_session_token_server_side(gateway):
    await gateway.login()
    token = gateway.client.cookies["forge_session"]
    assert (await gateway.client.post("/api/auth/logout", headers=CSRF)).status_code == 200

    # A copy of the cookie taken before logout (stolen, or another tab) no longer works.
    gateway.client.cookies.set("forge_session", token)
    assert (await gateway.client.get("/api/auth/me")).json() == {"authenticated": False}
    assert (await gateway.client.get("/api/keys")).status_code == 401

    gateway.client.cookies.clear()
    await gateway.login()  # a new login gets a new, working session
    assert (await gateway.client.get("/api/keys")).status_code == 200


async def test_key_names_are_trimmed_and_must_not_be_blank(gateway):
    await gateway.login()
    blank = await gateway.client.post("/api/keys", json={"name": "   "}, headers=CSRF)
    assert blank.status_code == 400
    assert "name" in blank.json()["error"]["message"]

    created = await gateway.client.post("/api/keys", json={"name": "  vigil  "}, headers=CSRF)
    assert created.json()["name"] == "vigil"
    dup = await gateway.client.post("/api/keys", json={"name": " vigil"}, headers=CSRF)
    assert dup.status_code == 409
    assert dup.json()["error"]["message"] == "A key named 'vigil' already exists"


async def test_huge_limits_are_a_400_not_a_database_error(gateway):
    await gateway.login()
    huge = await gateway.client.post("/api/keys", json={"name": "huge", "daily_token_quota": 10**19}, headers=CSRF)
    assert huge.status_code == 400
    key = await gateway.create_key("ok")
    patched = await gateway.client.patch(f"/api/keys/{key['id']}", json={"daily_token_quota": 2**63}, headers=CSRF)
    assert patched.status_code == 400


async def test_revoked_and_builtin_keys_cannot_be_patched(gateway):
    key = await gateway.create_key("old")
    await gateway.client.delete(f"/api/keys/{key['id']}", headers=CSRF)
    revoked = await gateway.client.patch(f"/api/keys/{key['id']}", json={"rpm_limit": 9}, headers=CSRF)
    assert revoked.status_code == 409

    listing = {k["name"]: k for k in (await gateway.client.get("/api/keys")).json()}
    console = listing["web-console"]
    assert console["limited"] is False and listing["old"]["limited"] is True
    builtin = await gateway.client.patch(f"/api/keys/{console['id']}", json={"rpm_limit": 9}, headers=CSRF)
    assert builtin.status_code == 400

    usage = (await gateway.client.get("/api/usage?range=24h")).json()
    today = {k["name"]: k for k in usage["today_by_key"]}
    assert today["web-console"]["daily_token_quota"] is None  # not enforced, so no quota bar
