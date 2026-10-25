"""The single Alembic migration creates the whole SPEC 2.5 schema."""

from __future__ import annotations

from conftest import fetch_rows


async def test_schema_matches_the_spec(database_url):
    tables = {
        r["tablename"]
        for r in await fetch_rows(database_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert {"api_keys", "usage_events", "conversations", "repos", "repo_chunks", "alembic_version"} <= tables

    extensions = {r["extname"] for r in await fetch_rows(database_url, "SELECT extname FROM pg_extension")}
    assert "vector" in extensions

    indexes = {
        r["indexname"]: r["indexdef"]
        for r in await fetch_rows(
            database_url, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
        )
    }
    assert "USING hnsw (embedding vector_cosine_ops)" in indexes["ix_repo_chunks_embedding_hnsw"]
    assert "ix_usage_events_ts" in indexes

    columns = await fetch_rows(
        database_url,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'api_keys'",
    )
    by_name = {c["column_name"]: c for c in columns}
    assert by_name["key_hash"]["is_nullable"] == "YES"  # NULL for the web-console row
    assert by_name["daily_token_quota"]["data_type"] == "bigint"
    assert by_name["revoked_at"]["data_type"] == "timestamp with time zone"

    embedding = await fetch_rows(
        database_url,
        "SELECT format_type(atttypid, atttypmod) AS type FROM pg_attribute "
        "WHERE attrelid = 'repo_chunks'::regclass AND attname = 'embedding'",
    )
    assert embedding[0]["type"] == "vector(768)"

    cascade = await fetch_rows(
        database_url,
        "SELECT confdeltype::text AS confdeltype FROM pg_constraint "
        "WHERE conrelid = 'repo_chunks'::regclass AND contype = 'f'",
    )
    assert cascade[0]["confdeltype"] == "c"


async def test_orm_metadata_covers_every_migrated_table(database_url):
    from app.db import Base

    migrated = {
        r["tablename"]
        for r in await fetch_rows(database_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert set(Base.metadata.tables) == migrated - {"alembic_version"}
