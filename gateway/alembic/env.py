"""Alembic environment: async engine, URL from FORGE_DATABASE_URL, metadata from app.models."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers every table, including the RAG ones, on Base.metadata)
from app.db import Base

config = context.config
target_metadata = Base.metadata

# Arbitrary constant: serialises concurrent migration runs (e.g. two Jobs racing on a rollout).
_MIGRATION_LOCK_ID = 7_301_955_001


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("FORGE_DATABASE_URL")
    if not url:
        raise RuntimeError("Set FORGE_DATABASE_URL (postgresql+asyncpg://...) to run migrations")
    return url


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _MIGRATION_LOCK_ID})
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
