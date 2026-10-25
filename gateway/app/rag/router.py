"""HTTP API for "Ask my repo" (SPEC 2.8), mounted at /api/repos, admin session only."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_admin
from app.config import get_settings
from app.db import async_session, get_session
from app.llm import LLM, get_llm
from app.rag.indexer import (
    Indexer,
    RepoUrlError,
    SessionFactory,
    UrlPolicy,
    describe_error,
    validate_branch,
    validate_repo_url,
)
from app.rag.models import Repo
from app.rag.retrieval import RetrievedChunk, search_chunks
from app.scheduler import Priority

router = APIRouter(prefix="/api/repos", tags=["repos"])

ANSWER_MAX_TOKENS = 1024
# Keeps the sources well inside the chat model's 8k context, leaving room for the answer.
MAX_SOURCE_CHARS = 18_000

SYSTEM_PROMPT = """You are a code assistant answering questions about the repository {url}.
You are given numbered source excerpts from that repository. Rules:
- Answer ONLY from the sources. Do not use outside knowledge about this repository.
- Cite every statement with the number of the source it comes from, like [1] or [2][4].
- Quote code exactly as it appears in the sources when you show it.
- If the sources do not contain the answer, say that the indexed code does not show it and
  name what is missing. Do not guess."""


# --------------------------------------------------------------------------- schemas


class RepoCreate(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    branch: str | None = Field(default=None, max_length=200)


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    branch: str | None
    status: str
    error: str | None
    files_indexed: int
    chunks: int
    commit_sha: str | None
    created_at: datetime
    updated_at: datetime


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=20)


# --------------------------------------------------------------------------- dependencies


def get_url_policy() -> UrlPolicy:
    """Production clone policy: https on FORGE_RAG_ALLOWED_HOSTS only. Tests override this."""
    return UrlPolicy.from_hosts(get_settings().rag_allowed_hosts)


def get_rag_redis(request: Request) -> Any:
    """The app's shared Redis client if the lifespan created one, else a lazily created client."""
    state = request.app.state
    shared = getattr(state, "redis", None)
    if shared is not None:
        return shared
    if getattr(state, "rag_redis", None) is None:
        from redis.asyncio import Redis

        state.rag_redis = Redis.from_url(get_settings().redis_url)
    return state.rag_redis


def get_session_factory() -> SessionFactory:
    """Background jobs and streamed answers outlive the request, so they open their own short
    sessions instead of holding the request-scoped one (and its pooled connection)."""
    return async_session


def get_indexer(
    request: Request,
    session_factory: SessionFactory = Depends(get_session_factory),
    llm: LLM = Depends(get_llm),
    redis: Any = Depends(get_rag_redis),
    policy: UrlPolicy = Depends(get_url_policy),
) -> Indexer:
    """One Indexer per app, so the 'one job at a time per gateway' rule holds."""
    indexer = getattr(request.app.state, "rag_indexer", None)
    if indexer is None:
        indexer = Indexer(session_factory=session_factory, llm=llm, redis=redis, policy=policy)
        request.app.state.rag_indexer = indexer
    return indexer


async def _get_repo(session: AsyncSession, repo_id: int) -> Repo:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found.")
    return repo


# --------------------------------------------------------------------------- CRUD


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RepoOut)
async def create_repo(
    body: RepoCreate,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    indexer: Indexer = Depends(get_indexer),
    policy: UrlPolicy = Depends(get_url_policy),
) -> Repo:
    try:
        url = validate_repo_url(body.url, policy)
        branch = validate_branch(body.branch)
    except RepoUrlError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if await session.scalar(select(Repo.id).where(Repo.url == url)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This repository is already added.")
    repo = Repo(url=url, branch=branch, status="queued")
    session.add(repo)
    try:
        await session.commit()
    except IntegrityError as exc:  # lost a race with an identical request
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This repository is already added.") from exc
    await session.refresh(repo)

    await indexer.submit(repo.id, principal)
    return repo


@router.get("", response_model=list[RepoOut])
async def list_repos(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)) -> list[Repo]:
    return list(await session.scalars(select(Repo).order_by(Repo.created_at.desc(), Repo.id.desc())))


@router.get("/{repo_id}", response_model=RepoOut)
async def get_repo(
    repo_id: int,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Repo:
    return await _get_repo(session, repo_id)


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repo(
    repo_id: int,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    indexer: Indexer = Depends(get_indexer),
) -> Response:
    repo = await _get_repo(session, repo_id)
    indexer.cancel(repo_id)
    await session.delete(repo)  # chunks go with it (ON DELETE CASCADE)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{repo_id}/reindex", status_code=status.HTTP_202_ACCEPTED, response_model=RepoOut)
async def reindex_repo(
    repo_id: int,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    indexer: Indexer = Depends(get_indexer),
) -> Repo:
    repo = await _get_repo(session, repo_id)
    # The claim lives in Redis, so a job queued or running on any gateway replica counts.
    if not await indexer.claim(repo_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This repository is already being indexed.")
    try:
        repo.status, repo.error = "queued", None
        await session.commit()
        await session.refresh(repo)
    except BaseException:
        await indexer.release(repo_id)
        raise
    indexer.enqueue(repo_id, principal)
    return repo


# --------------------------------------------------------------------------- ask (SSE)


def sse_event(event: str, data: Any) -> str:
    """One Server-Sent Event. json.dumps escapes newlines, so `data` is always one line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def select_sources(hits: list[RetrievedChunk], max_chars: int = MAX_SOURCE_CHARS) -> list[RetrievedChunk]:
    """Keep the best hits that fit the prompt budget (always at least the best one)."""
    chosen: list[RetrievedChunk] = []
    used = 0
    for hit in hits:
        if chosen and used + len(hit.content) > max_chars:
            break
        chosen.append(hit)
        used += len(hit.content)
    return chosen


def build_messages(repo_url: str, question: str, sources: list[RetrievedChunk]) -> list[dict]:
    blocks = [
        f"[{n}] {s.path}:{s.start_line}-{s.end_line}\n```\n{s.content}\n```" for n, s in enumerate(sources, start=1)
    ]
    user = (
        "Sources:\n\n"
        + "\n\n".join(blocks)
        + f"\n\nQuestion: {question}\n\nAnswer using only the sources above and cite them as [n]."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(url=repo_url)},
        {"role": "user", "content": user},
    ]


async def answer_events(
    *,
    repo_id: int,
    repo_url: str,
    question: str,
    top_k: int,
    llm: LLM,
    principal: Principal,
    session_factory: SessionFactory,
) -> AsyncIterator[str]:
    """sources -> token* -> done, or error at any point (the HTTP status is already 200)."""
    started = time.monotonic()
    try:
        [query_vector] = await llm.embed([question], principal=principal, priority=Priority.CHAT, input_type="query")
        async with session_factory() as session:
            hits = await search_chunks(session, repo_id, query_vector, top_k)
        if not hits:
            yield sse_event("error", {"message": "This repository has no indexed chunks."})
            return

        sources = select_sources(hits)
        yield sse_event(
            "sources",
            [
                {
                    "n": n,
                    "path": s.path,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "score": s.score,
                }
                for n, s in enumerate(sources, start=1)
            ],
        )

        stream = llm.stream_chat(
            build_messages(repo_url, question, sources),
            model_alias=llm.alias_for_role("chat"),
            principal=principal,
            priority=Priority.CHAT,
            max_tokens=ANSWER_MAX_TOKENS,
        )
        first_token_at: float | None = None
        answer_chars = 0
        async for delta in stream:
            if not delta:
                continue
            if first_token_at is None:
                first_token_at = time.monotonic()
            answer_chars += len(delta)
            yield sse_event("token", {"t": delta})

        finished = time.monotonic()
        usage: dict[str, Any] = {
            "sources": len(sources),
            "answer_chars": answer_chars,
            "duration_ms": round((finished - started) * 1000),
            "ttft_ms": round((first_token_at - started) * 1000) if first_token_at else None,
        }
        # Token counts from the model server (LLM.stream_chat's stream exposes them once read).
        reported = getattr(stream, "usage", None)
        if isinstance(reported, dict):
            usage.update(reported)
        yield sse_event("done", {"usage": usage})
    except Exception as exc:
        yield sse_event("error", {"message": describe_error(exc)})


@router.post("/{repo_id}/ask")
async def ask_repo(
    repo_id: int,
    body: AskRequest,
    principal: Principal = Depends(require_admin),
    llm: LLM = Depends(get_llm),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> StreamingResponse:
    async with session_factory() as session:
        repo = await _get_repo(session, repo_id)
    if repo.status != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Repository is not ready to answer questions (status: {repo.status})."
        )
    events = answer_events(
        repo_id=repo.id,
        repo_url=repo.url,
        question=body.question,
        top_k=body.top_k,
        llm=llm,
        principal=principal,
        session_factory=session_factory,
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        # no-transform and X-Accel-Buffering stop proxies from buffering the stream.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
