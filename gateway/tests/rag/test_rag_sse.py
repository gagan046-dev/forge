import json

import pytest

from app.rag import router as rag_router
from app.rag.retrieval import RetrievedChunk

from .rag_test_support import ADMIN, FakeLLM, parse_sse

HITS = [
    RetrievedChunk("src/auth.py", 1, 60, "# src/auth.py\ndef login(): ...", 0.91),
    RetrievedChunk("src/db.py", 51, 110, "# src/db.py\nengine = create()", 0.75),
]


class _NoSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


async def collect(llm, monkeypatch, hits=HITS, **kwargs) -> list[tuple[str, object]]:
    async def fake_search(session, repo_id, vector, top_k):
        assert top_k == kwargs.get("top_k", 8)
        return hits[:top_k]

    monkeypatch.setattr(rag_router, "search_chunks", fake_search)
    params = {
        "repo_id": 7,
        "repo_url": "https://github.com/acme/app",
        "question": "How does login work?",
        "top_k": 8,
        "llm": llm,
        "principal": ADMIN,
        "session_factory": _NoSession,
    } | kwargs
    body = "".join([chunk async for chunk in rag_router.answer_events(**params)])
    return parse_sse(body)


def test_sse_event_wire_format():
    frame = rag_router.sse_event("token", {"t": 'line one\nline "two" é'})
    assert frame.startswith("event: token\ndata: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n") == 3  # newlines inside the payload are JSON-escaped
    assert json.loads(frame.split("data: ", 1)[1]) == {"t": 'line one\nline "two" é'}


@pytest.mark.asyncio
async def test_answer_streams_sources_tokens_done(monkeypatch):
    llm = FakeLLM(answer=["Login ", "", "checks the hash [1]."])
    events = await collect(llm, monkeypatch)

    names = [name for name, _ in events]
    assert names == ["sources", "token", "token", "done"]  # empty deltas are not sent
    sources = events[0][1]
    assert sources == [
        {"n": 1, "path": "src/auth.py", "start_line": 1, "end_line": 60, "score": 0.91},
        {"n": 2, "path": "src/db.py", "start_line": 51, "end_line": 110, "score": 0.75},
    ]
    assert "".join(data["t"] for name, data in events if name == "token") == "Login checks the hash [1]."
    usage = events[-1][1]["usage"]
    assert usage["sources"] == 2 and usage["answer_chars"] == len("Login checks the hash [1].")
    assert usage["duration_ms"] >= 0 and usage["ttft_ms"] is not None

    # The question is embedded at interactive priority, and the chat goes to the chat alias.
    assert llm.embed_calls == [(1, 1)]
    (call,) = llm.chat_calls
    assert call["model_alias"] == "forge-chat" and call["priority"] == 1
    system, user = call["messages"]
    assert "ONLY from the sources" in system["content"] and "[1]" in system["content"]
    assert "https://github.com/acme/app" in system["content"]
    assert "[1] src/auth.py:1-60" in user["content"]
    assert "[2] src/db.py:51-110" in user["content"]
    assert user["content"].rstrip().endswith("cite them as [n].")
    assert "How does login work?" in user["content"]


@pytest.mark.asyncio
async def test_top_k_is_passed_to_retrieval(monkeypatch):
    events = await collect(FakeLLM(), monkeypatch, top_k=1)
    assert [s["n"] for s in events[0][1]] == [1]


@pytest.mark.asyncio
async def test_no_chunks_is_an_error_event(monkeypatch):
    events = await collect(FakeLLM(), monkeypatch, hits=[])
    assert events == [("error", {"message": "This repository has no indexed chunks."})]


@pytest.mark.asyncio
async def test_llm_failure_mid_stream_becomes_error_event(monkeypatch):
    class BrokenChat(FakeLLM):
        async def stream_chat(self, messages, **kwargs):
            yield "partial "
            raise RuntimeError("backend went away")

    events = await collect(BrokenChat(), monkeypatch)
    assert [name for name, _ in events] == ["sources", "token", "error"]
    assert events[-1][1] == {"message": "backend went away"}


@pytest.mark.asyncio
async def test_http_exception_detail_is_used_for_error_message(monkeypatch):
    from fastapi import HTTPException

    class QueueFull(FakeLLM):
        async def embed(self, texts, **kwargs):
            raise HTTPException(429, detail={"message": "GPU queue is full", "type": "queue_full"})

    events = await collect(QueueFull(), monkeypatch)
    assert events == [("error", {"message": "GPU queue is full"})]


def test_select_sources_respects_budget_but_keeps_best():
    big = [RetrievedChunk(f"f{i}.py", 1, 60, "x" * 10_000, 0.9 - i / 10) for i in range(3)]
    assert [s.path for s in rag_router.select_sources(big, max_chars=15_000)] == ["f0.py"]
    assert [s.path for s in rag_router.select_sources(big, max_chars=5_000)] == ["f0.py"]
    assert len(rag_router.select_sources(big, max_chars=30_000)) == 3
