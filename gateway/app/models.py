"""ORM models for the gateway tables (SPEC 2.5). The RAG tables live in app.rag.models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

# Imported for its side effect: registering the RAG tables on Base.metadata, so the whole
# schema is visible from one place (Alembic, tests).
import app.rag.models  # noqa: F401
from app.db import Base

WEB_CONSOLE_KEY_NAME = "web-console"


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    prefix: Mapped[str | None] = mapped_column(Text)
    # SHA-256 hex of the plain key; NULL for the built-in web-console row, so it can never match.
    key_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    rpm_limit: Mapped[int] = mapped_column(Integer)
    daily_token_quota: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    endpoint: Mapped[str] = mapped_column(Text)  # chat | completions | embeddings | rag
    model: Mapped[str] = mapped_column(Text)  # alias
    status: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    queue_ms: Mapped[int] = mapped_column(Integer, default=0)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    tokens_per_s: Mapped[float | None] = mapped_column(REAL)
    error_type: Mapped[str | None] = mapped_column(Text)

    # The usage page and per-key quota views filter by key over a time range.
    __table_args__ = (Index("ix_usage_events_key_id_ts", "key_id", "ts"),)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    title: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(Text, default="")
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
