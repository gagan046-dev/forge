"""Nearest-chunk search over `repo_chunks` with pgvector cosine distance."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.models import RepoChunk

# The HNSW index covers every repo, so a plain index scan followed by the `repo_id` filter
# can return fewer than k rows. Iterative scans (pgvector >= 0.8) keep scanning until k rows
# survive the filter; strict order keeps the results exactly sorted by distance.
_SEARCH_SETTINGS = (
    "SET LOCAL hnsw.ef_search = 100",
    "SET LOCAL hnsw.iterative_scan = strict_order",
)


@dataclass(frozen=True)
class RetrievedChunk:
    path: str
    start_line: int
    end_line: int
    content: str
    score: float  # cosine similarity, 1.0 = identical direction


async def search_chunks(
    session: AsyncSession, repo_id: int, query_embedding: list[float], top_k: int
) -> list[RetrievedChunk]:
    for statement in _SEARCH_SETTINGS:
        await session.execute(text(statement))

    distance = RepoChunk.embedding.cosine_distance(query_embedding).label("distance")
    rows = await session.execute(
        select(RepoChunk.path, RepoChunk.start_line, RepoChunk.end_line, RepoChunk.content, distance)
        .where(RepoChunk.repo_id == repo_id)
        .order_by(distance)
        .limit(top_k)
    )
    return [
        RetrievedChunk(
            path=row.path,
            start_line=row.start_line,
            end_line=row.end_line,
            content=row.content,
            score=round(1.0 - float(row.distance), 4),
        )
        for row in rows
    ]
