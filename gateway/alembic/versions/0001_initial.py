"""Initial schema: keys, usage, conversations and the RAG tables (SPEC 2.5).

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("prefix", sa.Text(), nullable=True),
        sa.Column("key_hash", sa.Text(), nullable=True, unique=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=False),
        sa.Column("daily_token_quota", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("builtin", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("key_id", sa.Integer(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("queue_ms", sa.Integer(), nullable=False),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("tokens_per_s", sa.REAL(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
    )
    op.create_index("ix_usage_events_ts", "usage_events", ["ts"])
    op.create_index("ix_usage_events_key_id_ts", "usage_events", ["key_id", "ts"])

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("messages", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])

    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("files_indexed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('queued', 'cloning', 'embedding', 'ready', 'failed')", name="ck_repos_status"),
    )

    op.create_table(
        "repo_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("repo_id", sa.Integer(), sa.ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
    )
    op.create_index("ix_repo_chunks_repo_id", "repo_chunks", ["repo_id"])
    op.create_index(
        "ix_repo_chunks_embedding_hnsw",
        "repo_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_table("repo_chunks")
    op.drop_table("repos")
    op.drop_table("conversations")
    op.drop_table("usage_events")
    op.drop_table("api_keys")
    # The vector extension is left in place: other objects may rely on it.
