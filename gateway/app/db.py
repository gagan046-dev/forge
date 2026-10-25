"""Database engine, session factory and the declarative Base shared by all ORM models."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str) -> AsyncEngine:
    """Create the process-wide engine. Called from the app lifespan (and by tools such as migrate)."""
    global _engine, _sessionmaker
    _engine = create_async_engine(database_url, pool_size=10, max_overflow=10, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        from app.config import get_settings

        init_db(get_settings().database_url)
    assert _engine is not None
    return _engine


def async_session() -> AsyncSession:
    """A new session, usable as `async with async_session() as session:` (e.g. in background tasks)."""
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with async_session() as session:
        yield session
