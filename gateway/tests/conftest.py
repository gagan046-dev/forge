"""Shared fixtures for the gateway tests.

Redis is fakeredis (with Lua via lupa); Ollama is the ASGI fake in fake_ollama.py. Tests that
start the app need a real pgvector Postgres, because the schema uses pgvector and Postgres-only
SQL. Locally:

    docker run -d --name forge-test-pg -e POSTGRES_USER=forge -e POSTGRES_PASSWORD=forge-test \
        -e POSTGRES_DB=forge_test -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg17-trixie

or point FORGE_TEST_DATABASE_URL at another server (CI uses a service container). The suite
creates its own `forge_gateway_test` database there and migrates it with `python -m app.migrate`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import fakeredis
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ADMIN_PASSWORD = "test-admin-password"
SESSION_SECRET = "test-session-secret-0123456789"

# app.config needs these when anything calls get_settings() (e.g. modules reading the default).
os.environ.setdefault("FORGE_ADMIN_PASSWORD", ADMIN_PASSWORD)
os.environ.setdefault("FORGE_SESSION_SECRET", SESSION_SECRET)

from fake_ollama import FakeOllama, SwitchableTransport  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SERVER_URL = os.environ.get(
    "FORGE_TEST_DATABASE_URL", "postgresql+asyncpg://forge:forge-test@127.0.0.1:55432/forge_test"
)
TEST_DB = "forge_gateway_test"
CSRF = {"X-Forge-Csrf": "1"}


def _asyncpg_dsn(url: str, database: str) -> str:
    return make_url(url).set(drivername="postgresql", database=database).render_as_string(hide_password=False)


async def _recreate_database() -> None:
    try:
        conn = await asyncpg.connect(_asyncpg_dsn(SERVER_URL, make_url(SERVER_URL).database or "postgres"))
    except (OSError, asyncpg.PostgresError) as exc:
        raise RuntimeError(
            f"Gateway tests need a pgvector Postgres at {SERVER_URL} (see tests/conftest.py): {exc}"
        ) from exc
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    """A freshly created, fully migrated test database (once per session)."""
    asyncio.run(_recreate_database())
    url = make_url(SERVER_URL).set(database=TEST_DB).render_as_string(hide_password=False)
    subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        cwd=GATEWAY_DIR,
        env={**os.environ, "FORGE_DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return url


async def _truncate(database_url: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(database_url, TEST_DB))
    try:
        await conn.execute(
            "TRUNCATE usage_events, api_keys, conversations, repo_chunks, repos RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()


@pytest.fixture
def fake_server() -> fakeredis.FakeServer:
    """One fake Redis server; extra clients on it act like other gateway replicas."""
    return fakeredis.FakeServer()


@pytest_asyncio.fixture
async def redis(fake_server) -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def settings_overrides() -> dict:
    """Override per test module/test to tweak the gateway configuration."""
    return {}


@dataclass
class Gateway:
    app: object
    client: httpx.AsyncClient
    ollama: FakeOllama
    redis: fakeredis.FakeAsyncRedis
    settings: Settings

    @property
    def state(self):
        return self.app.state  # type: ignore[attr-defined]

    async def login(self) -> None:
        response = await self.client.post("/api/auth/login", json={"password": ADMIN_PASSWORD}, headers=CSRF)
        assert response.status_code == 200, response.text

    async def create_key(self, name: str = "test-key", **limits: int) -> dict:
        """Create an API key through the admin API (logging in first if needed)."""
        if "forge_session" not in self.client.cookies:
            await self.login()
        response = await self.client.post("/api/keys", json={"name": name, **limits}, headers=CSRF)
        assert response.status_code == 201, response.text
        return response.json()


@pytest_asyncio.fixture
async def gateway(database_url, redis, fake_ollama, settings_overrides) -> AsyncIterator[Gateway]:
    await _truncate(database_url)
    settings = Settings(
        database_url=database_url,
        redis_url="redis://unused",
        ollama_url="http://ollama.test:11434",
        gpu_exporter_url="http://gpu.test:9400",
        admin_password=ADMIN_PASSWORD,
        session_secret=SESSION_SECRET,
        env="dev",
        **settings_overrides,
    )
    app = create_app(settings, redis=redis, http_transport=SwitchableTransport(fake_ollama))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://forge.test") as client:
            yield Gateway(app=app, client=client, ollama=fake_ollama, redis=redis, settings=settings)


@pytest.fixture
def bearer():
    def headers(key: dict) -> dict[str, str]:
        return {"Authorization": f"Bearer {key['key']}"}

    return headers


async def fetch_rows(database_url: str, sql: str, *args) -> list[asyncpg.Record]:
    """Read straight from the test database (independent of the app's own sessions)."""
    conn = await asyncpg.connect(_asyncpg_dsn(database_url, TEST_DB))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def usage_events(database_url: str) -> list[asyncpg.Record]:
    return await fetch_rows(database_url, "SELECT * FROM usage_events ORDER BY id")
