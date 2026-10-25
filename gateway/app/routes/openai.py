"""OpenAI-compatible API (SPEC 2.3): /v1/models, /v1/chat/completions, /v1/completions, /v1/embeddings."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth import Principal, require_principal
from app.config import ModelSpec
from app.errors import ForgeError, invalid_request
from app.llm import LLM, get_llm
from app.metering import CallRecord, estimate_tokens
from app.scheduler import Priority

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])


# Request bodies are validated for the fields Forge relies on; every other OpenAI field is passed
# through to Ollama untouched.
class _Body(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1)


class ChatBody(_Body):
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


class CompletionBody(_Body):
    prompt: str
    suffix: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    stop: str | list[str] | None = None
    stream: bool = False


class EmbeddingBody(_Body):
    input: str | list[str]


async def _parse[BodyT: _Body](
    request: Request, schema: type[BodyT], *, endpoint: str, principal: Principal, llm: LLM
) -> tuple[BodyT, dict[str, Any], ModelSpec]:
    """Validate the body and resolve the model; refusals are still recorded as usage events."""
    raw: Any = None
    try:
        try:
            raw = await request.json()
        except ValueError:
            raise invalid_request("Request body must be JSON") from None
        if not isinstance(raw, dict):
            raise invalid_request("Request body must be a JSON object")
        try:
            body = schema.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise invalid_request(f"{where}: {first['msg']}" if where else first["msg"]) from None
        return body, raw, llm.resolve(body.model)
    except ForgeError as err:
        model = raw.get("model") if isinstance(raw, dict) and isinstance(raw.get("model"), str) else ""
        await llm.record_rejection(principal, endpoint=endpoint, model=model, err=err)
        raise


def _headers(request: Request, record: CallRecord, spec: ModelSpec) -> dict[str, str]:
    return {
        "x-request-id": request.state.request_id,
        "x-forge-queue-ms": str(record.queue_ms),
        "x-forge-model": spec.name,
    }


async def _sse(chunks: AsyncGenerator[dict[str, Any], None], alias: str) -> AsyncIterator[str]:
    """Re-emit Ollama's chunks as SSE with the model name mapped back to the alias."""
    try:
        async for chunk in chunks:
            if "model" in chunk:
                chunk["model"] = alias
            yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
        yield "data: [DONE]\n\n"
    except ForgeError as err:
        # Headers are already sent, so report mid-stream failures in-band, like OpenAI does.
        yield f"data: {json.dumps(err.to_body())}\n\n"
    except Exception:
        log.exception("stream failed")
        yield f"data: {json.dumps(ForgeError(500, 'server_error', 'Stream failed').to_body())}\n\n"
    finally:
        await chunks.aclose()


async def _proxy(
    request: Request,
    llm: LLM,
    principal: Principal,
    *,
    endpoint: str,
    spec: ModelSpec,
    priority: Priority,
    path: str,
    upstream: dict[str, Any],
    stream: bool,
    prompt_estimate: int,
) -> Response:
    upstream["model"] = spec.name
    if stream:
        # Ask Ollama for a final usage chunk so streamed requests are metered exactly.
        upstream["stream_options"] = {**(upstream.get("stream_options") or {}), "include_usage": True}
        record, chunks = await llm.open_stream(
            principal,
            endpoint=endpoint,
            spec=spec,
            priority=priority,
            path=path,
            body=upstream,
            prompt_estimate=prompt_estimate,
        )
        headers = _headers(request, record, spec) | {"cache-control": "no-cache", "x-accel-buffering": "no"}
        return StreamingResponse(_sse(chunks, spec.alias), media_type="text/event-stream", headers=headers)

    data, record = await llm.request_json(
        principal,
        endpoint=endpoint,
        spec=spec,
        priority=priority,
        path=path,
        body=upstream,
        prompt_estimate=prompt_estimate,
    )
    data["model"] = spec.alias
    return JSONResponse(data, headers=_headers(request, record, spec))


@router.get("/models")
async def list_models(_: Principal = Depends(require_principal), llm: LLM = Depends(get_llm)) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": m.alias, "object": "model", "owned_by": "forge", "role": m.role} for m in llm.models],
    }


@router.post("/chat/completions")
async def chat_completions(
    request: Request, principal: Principal = Depends(require_principal), llm: LLM = Depends(get_llm)
) -> Response:
    body, raw, spec = await _parse(request, ChatBody, endpoint="chat", principal=principal, llm=llm)
    prompt_estimate = sum(estimate_tokens(str(m.get("content") or "")) for m in body.messages)
    return await _proxy(
        request,
        llm,
        principal,
        endpoint="chat",
        spec=spec,
        priority=Priority.CHAT,
        path="/v1/chat/completions",
        upstream=raw,
        stream=body.stream,
        prompt_estimate=prompt_estimate,
    )


@router.post("/completions")
async def completions(
    request: Request, principal: Principal = Depends(require_principal), llm: LLM = Depends(get_llm)
) -> Response:
    body, raw, spec = await _parse(request, CompletionBody, endpoint="completions", principal=principal, llm=llm)
    # Autocomplete models get the top priority (and supersession); anything else waits like chat.
    priority = Priority.AUTOCOMPLETE if spec.role == "autocomplete" else Priority.CHAT
    prompt_estimate = estimate_tokens(body.prompt) + estimate_tokens(body.suffix or "")
    return await _proxy(
        request,
        llm,
        principal,
        endpoint="completions",
        spec=spec,
        priority=priority,
        path="/v1/completions",
        upstream=raw,
        stream=body.stream,
        prompt_estimate=prompt_estimate,
    )


@router.post("/embeddings")
async def embeddings(
    request: Request, principal: Principal = Depends(require_principal), llm: LLM = Depends(get_llm)
) -> Response:
    body, raw, spec = await _parse(request, EmbeddingBody, endpoint="embeddings", principal=principal, llm=llm)
    texts = [body.input] if isinstance(body.input, str) else body.input
    return await _proxy(
        request,
        llm,
        principal,
        endpoint="embeddings",
        spec=spec,
        priority=Priority.BATCH,
        path="/v1/embeddings",
        upstream=raw,
        stream=False,
        prompt_estimate=sum(estimate_tokens(t) for t in texts),
    )
