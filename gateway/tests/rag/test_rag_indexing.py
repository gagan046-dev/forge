"""End to end: API -> background job -> git clone of a local repo -> chunks in pgvector -> ask."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.rag import router as rag_router
from app.rag.indexer import LOCK_NAME, UrlPolicy, cluster_lock, index_repo
from app.rag.models import Repo, RepoChunk

from .rag_test_support import ADMIN, parse_sse

GIT_IDENTITY = ["-c", "user.name=Forge Test", "-c", "user.email=test@forge.invalid"]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    files = {
        "README.md": "# Demo\n\nA tiny service that greets people.\n",
        "src/greet.py": "def greet(name):\n    return f'hello {name}'\n"
        + "\n".join(f"# filler line {i}" for i in range(1, 100)),
        "src/config.yaml": "port: 8080\n",
        "node_modules/lib/index.js": "module.exports = 1\n",
        "package-lock.json": "{}\n",
    }
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    (repo / "logo.py").write_bytes(b"\x89PNG\x00\x00binary")
    git(repo, "add", "-A")
    # A symlink pointing outside the checkout, committed without needing OS symlink support.
    target_blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="../../outside-secret.txt",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{target_blob},escape.txt")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def origin(tmp_path) -> Path:
    (tmp_path / "outside-secret.txt").write_text("TOP SECRET\n")
    return make_repo(tmp_path)


async def wait_idle(rag_app) -> None:
    await rag_app.state.rag_indexer.join()


async def chunk_rows(session_factory, repo_id: int) -> list[RepoChunk]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(RepoChunk).where(RepoChunk.repo_id == repo_id).order_by(RepoChunk.path, RepoChunk.start_line)
        )
        return list(rows)


@pytest.mark.asyncio
async def test_index_reindex_ask_delete(client, rag_app, origin, session_factory, fake_llm, fake_redis):
    response = await client.post("/api/repos", json={"url": origin.as_uri(), "branch": "main"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "queued" and created["branch"] == "main"
    repo_id = created["id"]

    await wait_idle(rag_app)
    repo = (await client.get(f"/api/repos/{repo_id}")).json()
    assert repo["status"] == "ready", repo["error"]
    assert repo["error"] is None
    assert repo["commit_sha"] == git(origin, "rev-parse", "HEAD")
    # escape.txt is checked out as a plain text file holding the link target, never followed.
    assert repo["files_indexed"] == 4  # README.md, escape.txt, src/config.yaml, src/greet.py

    rows = await chunk_rows(session_factory, repo_id)
    assert repo["chunks"] == len(rows) == 5  # greet.py has 101 lines -> 2 chunks
    paths = {r.path for r in rows}
    assert paths == {"README.md", "escape.txt", "src/config.yaml", "src/greet.py"}
    assert all("TOP SECRET" not in r.content for r in rows)
    greet = [(r.start_line, r.end_line) for r in rows if r.path == "src/greet.py"]
    assert greet == [(1, 60), (51, 101)]
    assert all(r.content.startswith(f"# {r.path}\n") for r in rows)
    assert all(size <= 32 and priority == 2 for size, priority in fake_llm.embed_calls)
    assert not await fake_redis.exists(LOCK_NAME)  # the cluster lock was released

    # Ask: SSE with sources first; the best source for a greeting question is greet.py.
    async with client.stream(
        "POST", f"/api/repos/{repo_id}/ask", json={"question": "how does greet return hello name", "top_k": 3}
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        body = "".join([part async for part in stream.aiter_text()])
    events = parse_sse(body)
    assert events[0][0] == "sources" and events[-1][0] == "done"
    sources = events[0][1]
    assert len(sources) == 3 and sources[0]["path"] == "src/greet.py" and sources[0]["n"] == 1
    assert {"n", "path", "start_line", "end_line", "score"} == set(sources[0])

    # Re-index after a new commit: chunks are replaced, not appended.
    (origin / "src" / "greet.py").write_text("def greet(name):\n    return 'hi ' + name\n")
    # Stage just this file: `commit -a` would drop escape.txt, which only exists in the index.
    git(origin, "add", "src/greet.py")
    git(origin, "commit", "-q", "-m", "shorter")
    response = await client.post(f"/api/repos/{repo_id}/reindex")
    assert response.status_code == 202 and response.json()["status"] == "queued"
    await wait_idle(rag_app)
    repo = (await client.get(f"/api/repos/{repo_id}")).json()
    assert repo["status"] == "ready" and repo["commit_sha"] == git(origin, "rev-parse", "HEAD")
    rows = await chunk_rows(session_factory, repo_id)
    assert len(rows) == repo["chunks"] == 4
    assert [r.content for r in rows if r.path == "src/greet.py"] == [
        "# src/greet.py\ndef greet(name):\n    return 'hi ' + name"
    ]

    listing = (await client.get("/api/repos")).json()
    assert [r["id"] for r in listing] == [repo_id]

    assert (await client.delete(f"/api/repos/{repo_id}")).status_code == 204
    assert (await client.get(f"/api/repos/{repo_id}")).status_code == 404
    assert await chunk_rows(session_factory, repo_id) == []


@pytest.mark.asyncio
async def test_duplicate_url_and_policy_errors(client, rag_app, origin):
    first = await client.post("/api/repos", json={"url": origin.as_uri()})
    assert first.status_code == 201
    duplicate = await client.post("/api/repos", json={"url": origin.as_uri() + "/"})
    assert duplicate.status_code == 409
    bad_branch = await client.post("/api/repos", json={"url": "https://github.com/a/b", "branch": "--evil"})
    assert bad_branch.status_code == 400

    # With the production policy, file:// (and anything but https on allowed hosts) is refused.
    rag_app.dependency_overrides[rag_router.get_url_policy] = lambda: UrlPolicy.from_hosts("github.com")
    for url in [origin.as_uri(), "http://github.com/a/b", "https://gitlab.com/a/b", "git@github.com:a/b"]:
        response = await client.post("/api/repos", json={"url": url})
        assert response.status_code == 400, url
    await wait_idle(rag_app)


@pytest.mark.asyncio
async def test_ask_and_reindex_rules(client, rag_app, session_factory):
    async with session_factory() as session:
        repo = Repo(url="https://github.com/acme/pending", status="cloning")
        session.add(repo)
        await session.commit()
    response = await client.post(f"/api/repos/{repo.id}/ask", json={"question": "anything"})
    assert response.status_code == 409
    assert (await client.post("/api/repos/9999/ask", json={"question": "x"})).status_code == 404
    assert (await client.post(f"/api/repos/{repo.id}/ask", json={"question": ""})).status_code == 422
    assert (await client.post(f"/api/repos/{repo.id}/ask", json={"question": "x", "top_k": 99})).status_code == 422
    assert (await client.post("/api/repos/9999/reindex")).status_code == 404
    assert (await client.delete("/api/repos/9999")).status_code == 404


@pytest.mark.asyncio
async def test_clone_failure_marks_repo_failed(client, rag_app, tmp_path):
    missing = (tmp_path / "does-not-exist").as_uri()
    repo_id = (await client.post("/api/repos", json={"url": missing})).json()["id"]
    await wait_idle(rag_app)
    repo = (await client.get(f"/api/repos/{repo_id}")).json()
    assert repo["status"] == "failed"
    assert repo["error"].startswith("git failed:")


@pytest.mark.asyncio
async def test_unknown_branch_marks_repo_failed(client, rag_app, origin):
    repo_id = (await client.post("/api/repos", json={"url": origin.as_uri(), "branch": "nope"})).json()["id"]
    await wait_idle(rag_app)
    repo = (await client.get(f"/api/repos/{repo_id}")).json()
    assert repo["status"] == "failed" and "nope" in repo["error"]


@pytest.mark.asyncio
async def test_embedding_failure_marks_repo_failed(client, rag_app, origin, fake_llm):
    fake_llm.fail_embed_after = 0
    repo_id = (await client.post("/api/repos", json={"url": origin.as_uri()})).json()["id"]
    await wait_idle(rag_app)
    repo = (await client.get(f"/api/repos/{repo_id}")).json()
    assert repo["status"] == "failed" and repo["error"] == "embedding backend unavailable"


@pytest.mark.asyncio
async def test_wrong_embedding_size_marks_repo_failed(session_factory, origin, test_policy):
    class ShortVectors:
        async def embed(self, texts, *, principal, priority=2, input_type=None):
            return [[0.1] * 10 for _ in texts]

    async with session_factory() as session:
        repo = Repo(url=origin.as_uri(), status="queued")
        session.add(repo)
        await session.commit()
    await index_repo(repo.id, session_factory=session_factory, llm=ShortVectors(), principal=ADMIN, policy=test_policy)
    async with session_factory() as session:
        stored = await session.get(Repo, repo.id)
        count = await session.scalar(select(func.count()).where(RepoChunk.repo_id == repo.id))
    assert stored.status == "failed" and "768 dimensions" in stored.error
    assert count == 0


@pytest.mark.asyncio
async def test_job_waits_for_the_cluster_lock(client, rag_app, origin, fake_redis):
    import asyncio

    async with cluster_lock(fake_redis):  # another gateway is indexing
        repo_id = (await client.post("/api/repos", json={"url": origin.as_uri()})).json()["id"]
        await asyncio.sleep(0.5)
        assert (await client.get(f"/api/repos/{repo_id}")).json()["status"] == "queued"
        # While queued here, a second reindex request is refused instead of piling up.
        assert (await client.post(f"/api/repos/{repo_id}/reindex")).status_code == 409
    await wait_idle(rag_app)
    assert (await client.get(f"/api/repos/{repo_id}")).json()["status"] == "ready"


@pytest.mark.asyncio
async def test_delete_while_waiting_cancels_the_job(client, rag_app, origin, fake_redis, fake_llm):
    import asyncio

    async with cluster_lock(fake_redis):
        repo_id = (await client.post("/api/repos", json={"url": origin.as_uri()})).json()["id"]
        await asyncio.sleep(0.2)
        assert (await client.delete(f"/api/repos/{repo_id}")).status_code == 204
    await wait_idle(rag_app)
    assert fake_llm.embed_calls == []


@pytest.mark.asyncio
async def test_reindex_is_refused_while_another_replica_holds_the_job(client, rag_app, origin, fake_redis):
    from app.rag.indexer import JOB_CLAIM_KEY

    repo_id = (await client.post("/api/repos", json={"url": origin.as_uri()})).json()["id"]
    await wait_idle(rag_app)
    assert (await client.get(f"/api/repos/{repo_id}")).json()["status"] == "ready"

    # Another gateway replica (same Redis, its own in-memory queue) is indexing this repo.
    other = fake_redis.lock(JOB_CLAIM_KEY.format(repo_id=repo_id), timeout=60, thread_local=False)
    assert await other.acquire(blocking=False)
    refused = await client.post(f"/api/repos/{repo_id}/reindex")
    assert refused.status_code == 409
    # The refused request must not touch the row the other replica is working on.
    assert (await client.get(f"/api/repos/{repo_id}")).json()["status"] == "ready"

    await other.release()
    assert (await client.post(f"/api/repos/{repo_id}/reindex")).status_code == 202
    await wait_idle(rag_app)
    # The claim is released when the job ends, so the next reindex is accepted again.
    assert (await client.post(f"/api/repos/{repo_id}/reindex")).status_code == 202
    await wait_idle(rag_app)


@pytest.mark.asyncio
async def test_two_indexers_on_one_redis_share_the_claim(session_factory, fake_llm, fake_redis, test_policy):
    from app.rag.indexer import Indexer

    first = Indexer(session_factory=session_factory, llm=fake_llm, redis=fake_redis, policy=test_policy)
    second = Indexer(session_factory=session_factory, llm=fake_llm, redis=fake_redis, policy=test_policy)
    try:
        assert await first.claim(42)
        assert not await second.claim(42)
        await first.release(42)
        assert await second.claim(42)
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_delete_from_another_replica_stops_the_job(session_factory, tmp_path, test_policy, caplog):
    from sqlalchemy import delete

    big = tmp_path / "big"
    big.mkdir()
    git(big, "init", "-q", "-b", "main")
    for i in range(80):  # 80 chunks: three embed batches of 32
        (big / f"mod{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    git(big, "add", "-A")
    git(big, "commit", "-q", "-m", "many files")

    class DeletingLLM:
        """Embeds the first batch, during which 'another replica' deletes the repo."""

        def __init__(self) -> None:
            self.calls = 0

        async def embed(self, texts, *, principal, priority=2, input_type=None):
            self.calls += 1
            async with session_factory() as session:
                await session.execute(delete(Repo).where(Repo.id == repo.id))
                await session.commit()
            return [[0.0] * 767 + [1.0] for _ in texts]

    async with session_factory() as session:
        repo = Repo(url=big.as_uri(), status="queued")
        session.add(repo)
        await session.commit()
    llm = DeletingLLM()
    with caplog.at_level("INFO", logger="app.rag.indexer"):
        await index_repo(repo.id, session_factory=session_factory, llm=llm, principal=ADMIN, policy=test_policy)
    assert llm.calls == 1  # stopped instead of embedding every remaining batch
    assert "deleted while indexing" in caplog.text
    assert "failed" not in caplog.text


@pytest.mark.asyncio
async def test_chunks_and_questions_are_embedded_with_their_task_types(client, rag_app, origin, fake_llm):
    repo_id = (await client.post("/api/repos", json={"url": origin.as_uri()})).json()["id"]
    await wait_idle(rag_app)
    assert set(fake_llm.embed_input_types) == {"document"}
    fake_llm.embed_input_types.clear()
    await client.post(f"/api/repos/{repo_id}/ask", json={"question": "How does greet work?"})
    assert fake_llm.embed_input_types == ["query"]
