"""Fixtures for the RAG tests.

The RAG router runs in a bare FastAPI app with its request-time dependencies (admin session,
DB session, LLM, Redis, URL policy) overridden, so these tests exercise the RAG code itself.

Database tests need a pgvector Postgres. Locally:
    docker run -d --name forge-test-pg-rag -e POSTGRES_USER=forge -e POSTGRES_PASSWORD=forge-test \
        -e POSTGRES_DB=forge_rag_test -p 127.0.0.1:55433:5432 pgvector/pgvector:0.8.7-pg17
or point FORGE_TEST_RAG_DATABASE_URL at another one (CI uses its service container).
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from app.auth import require_admin
from app.db import Base, get_session
from app.llm import get_llm
from app.rag import router as rag_router
from app.rag.indexer import UrlPolicy
from app.rag.models import Repo, RepoChunk

from .rag_test_support import ADMIN, FakeLLM

TEST_DATABASE_URL = os.environ.get(
    "FORGE_TEST_RAG_DATABASE_URL",
    "postgresql+asyncpg://forge:forge-test@127.0.0.1:55433/forge_rag_test",
)

# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def pg_engine():
    """A fresh repos/repo_chunks schema in the test database."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL)
    tables = [Repo.__table__, RepoChunk.__table__]
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=tables))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    except (OSError, ConnectionError) as exc:
        await engine.dispose()
        pytest.skip(f"pgvector Postgres not reachable at {TEST_DATABASE_URL}: {exc}")
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest_asyncio.fixture
async def fake_redis():
    from fakeredis import FakeAsyncRedis

    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
def test_policy() -> UrlPolicy:
    # file:// is allowed here, and only here, so tests can index a local repository.
    return UrlPolicy(allowed_hosts=frozenset({"github.com"}), allow_file=True)


@pytest_asyncio.fixture
async def rag_app(session_factory, fake_llm, fake_redis, test_policy):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(rag_router.router)

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[rag_router.get_session_factory] = lambda: session_factory
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_llm] = lambda: fake_llm
    app.dependency_overrides[rag_router.get_rag_redis] = lambda: fake_redis
    app.dependency_overrides[rag_router.get_url_policy] = lambda: test_policy
    yield app
    indexer = getattr(app.state, "rag_indexer", None)
    if indexer is not None:
        await indexer.aclose()


@pytest_asyncio.fixture
async def client(rag_app):
    import httpx

    transport = httpx.ASGITransport(app=rag_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://forge.test") as http:
        yield http
