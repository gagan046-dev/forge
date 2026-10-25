"""Retrieval against a real pgvector Postgres (see conftest for how to start one)."""

import math

import pytest
from sqlalchemy import text

from app.rag.models import Repo, RepoChunk
from app.rag.retrieval import search_chunks


def unit(*components: float, dims: int = 768) -> list[float]:
    vector = list(components) + [0.0] * (dims - len(components))
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


async def add_repo(session_factory, url: str, vectors: dict[str, list[float]]) -> int:
    async with session_factory() as session:
        repo = Repo(url=url, status="ready")
        session.add(repo)
        await session.flush()
        session.add_all(
            RepoChunk(repo_id=repo.id, path=path, start_line=1, end_line=60, content=f"# {path}", embedding=vec)
            for path, vec in vectors.items()
        )
        await session.commit()
        return repo.id


@pytest.mark.asyncio
async def test_orders_by_cosine_similarity_and_scores(session_factory):
    repo_id = await add_repo(
        session_factory,
        "https://github.com/acme/one",
        {
            "exact.py": unit(1, 0),
            "close.py": unit(1, 0.2),
            "orthogonal.py": unit(0, 1),
            "opposite.py": unit(-1, 0),
        },
    )
    async with session_factory() as session:
        hits = await search_chunks(session, repo_id, unit(1, 0), top_k=3)

    assert [h.path for h in hits] == ["exact.py", "close.py", "orthogonal.py"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-4)
    assert hits[1].score == pytest.approx(1 / math.sqrt(1.04), abs=1e-4)
    assert hits[2].score == pytest.approx(0.0, abs=1e-4)
    assert hits[0].content == "# exact.py" and (hits[0].start_line, hits[0].end_line) == (1, 60)


@pytest.mark.asyncio
async def test_only_searches_the_requested_repo(session_factory):
    mine = await add_repo(session_factory, "https://github.com/acme/mine", {"mine.py": unit(0, 1)})
    await add_repo(session_factory, "https://github.com/acme/other", {"other.py": unit(1, 0)})
    async with session_factory() as session:
        hits = await search_chunks(session, mine, unit(1, 0), top_k=5)
    assert [h.path for h in hits] == ["mine.py"]


@pytest.mark.asyncio
async def test_small_repo_still_gets_k_results_next_to_a_large_one(session_factory):
    # The HNSW index spans every repo; without iterative scans the repo filter would discard
    # most candidates and return fewer than k rows.
    big = {f"big/{i}.py": unit(1, i / 1000) for i in range(400)}
    await add_repo(session_factory, "https://github.com/acme/big", big)
    small = await add_repo(
        session_factory,
        "https://github.com/acme/small",
        {f"small/{i}.py": unit(0, 1, i / 10) for i in range(10)},
    )
    async with session_factory() as session:
        # ANALYZE so the planner actually picks the HNSW index, as it would on a real table.
        await session.execute(text("ANALYZE repo_chunks"))
        await session.commit()
        hits = await search_chunks(session, small, unit(1, 0), top_k=8)
    assert len(hits) == 8
    assert all(h.path.startswith("small/") for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


@pytest.mark.asyncio
async def test_schema_has_hnsw_cosine_index_and_cascade(session_factory):
    async with session_factory() as session:
        indexdef = await session.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_repo_chunks_embedding_hnsw'")
        )
        assert "USING hnsw" in indexdef and "vector_cosine_ops" in indexdef
        coltype = await session.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'repo_chunks'::regclass AND attname = 'embedding'"
            )
        )
        assert coltype == "vector(768)"

    repo_id = await add_repo(session_factory, "https://github.com/acme/gone", {"a.py": unit(1)})
    async with session_factory() as session:
        await session.delete(await session.get(Repo, repo_id))
        await session.commit()
        remaining = await session.scalar(text("SELECT count(*) FROM repo_chunks WHERE repo_id = :r"), {"r": repo_id})
    assert remaining == 0
