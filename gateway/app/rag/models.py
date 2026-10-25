"""ORM models for the RAG tables (SPEC 2.5: `repos`, `repo_chunks`)."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIMENSIONS = 768

REPO_STATUSES = ("queued", "cloning", "embedding", "ready", "failed")


class Repo(Base):
    # Deleting a repo removes its chunks through the database-level ON DELETE CASCADE.
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    branch: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    files_indexed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    chunks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    commit_sha: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in REPO_STATUSES) + ")",
            name="ck_repos_status",
        ),
    )


class RepoChunk(Base):
    __tablename__ = "repo_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Indexed so re-indexing and deleting a repo's chunks doesn't scan the whole table.
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))

    __table_args__ = (
        Index(
            "ix_repo_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
