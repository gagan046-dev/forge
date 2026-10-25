"""OpenAI-compatible API (SPEC 2.3): /v1/models, /v1/chat/completions, /v1/completions, /v1/embeddings."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.asyncutil import cancel_on_disconnect
from app.auth import Principal, require_principal
from app.config import ModelSpec
from app.errors import ForgeError, invalid_request
from app.llm import LLM, get_llm
from app.metering import CallRecord, estimate_tokens
from app.scheduler import Priority

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

# Which model roles each endpoint can serve. A chat or autocomplete model can't embed and an
# embedding model can't generate; catching that here gives the client a 400 before it queues,
# instead of an upstream error that looks like a server fault.
_GENERATIVE = frozenset({"chat", "autocomplete"})
_EMBEDDING = frozenset({"embedding"})


# Request bodies are validated strictly (no "yes" -> True or "0.5" -> 0.5 coercion) for the fields
# Forge and Ollama care about, because the raw body is what gets forwarded: anything accepted here
# must be something Ollama accepts too. Unknown OpenAI fields pass through untouched.
class _Body(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    model: str = Field(min_length=1)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    include_usage: bool = False


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    role: Literal["system", "user", "assistant", "tool"]
    content: Any = None

    @field_validator("content")
    @classmethod
    def _text_or_parts(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(part, dict) for part in value):
            return value
        raise ValueError("must be a string or a list of content parts")


class _Generation(_Body):
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    seed: int | None = None
    stop: Any = None

    @field_validator("stop")
    @classmethod
    def _stop_strings(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        raise ValueError("must be a string or a list of strings")

    @property
    def wants_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage


class ChatBody(_Generation):
    messages: list[ChatMessage] = Field(min_length=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)


class CompletionBody(_Generation):
    prompt: Any
    suffix: str | None = None

    @field_validator("prompt")
    @classmethod
    def _single_prompt(cls, value: Any) -> str:
        # OpenAI also takes a list of prompts (one completion each); Ollama takes one string.
        # A one-element list is unwrapped so clients that always send a list still work.
        if isinstance(value, str):
            return value
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        if isinstance(value, list) and all(isinstance(p, str) for p in value):
            raise ValueError("only one prompt per request is supported; send a string or a one-element list")
        raise ValueError("must be a string")


class EmbeddingBody(_Body):
    input: Any

    @field_validator("input")
    @classmethod
    def _non_empty_texts(cls, value: Any) -> list[str] | str:
        if isinstance(value, str):
            if not value:
                raise ValueError("must not be an empty string")
            return value
        if isinstance(value, list) and all(isinstance(t, str) for t in value):
            if not value:
                raise ValueError("must not be an empty list")
            if any(not t for t in value):
                raise ValueError("must not contain empty strings")
            return value
        raise ValueError("must be a string or a list of strings")

    @property
    def texts(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else self.input


def _message_text(message: ChatMessage) -> str:
    """The text of a message for token estimation (text parts only; images are not counted)."""
    if isinstance(message.content, str):
        return message.content
    if isinstance(message.content, list):
        return "".join(str(p.get("text") or "") for p in message.content if isinstance(p, dict))
    return ""


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    message = first["msg"].removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


async def _prepare[BodyT: _Body](
    request: Request,
    schema: type[BodyT],
    *,
    endpoint: str,
    roles: frozenset[str],
    estimate: Callable[[BodyT], int],
    principal: Principal,
    llm: LLM,
) -> tuple[BodyT, dict[str, Any], ModelSpec, int]:
    """Validate the body, resolve the model and check it fits; refusals are still recorded.

    Returns (body, raw body to forward, model, estimated prompt tokens).
    """
    raw: Any = None
    spec: ModelSpec | None = None
    try:
        try:
            raw = await request.json()
        except ValueError:
            raise invalid_request("Request body must be JSON") from None
        if not isinstance(raw, dict):
            raise invalid_request("Request body must be a JSON object")
        # Resolve first, so a refusal for a bad body is still attributed to the right model.
        if isinstance(raw.get("model"), str):
            spec = llm.lookup(raw["model"])
        try:
            body = schema.model_validate(raw)
        except ValidationError as exc:
            raise invalid_request(_validation_message(exc)) from None
        spec = llm.resolve(body.model)
        if spec.role not in roles:
            raise invalid_request(
                f"Model '{spec.alias}' is {'an' if spec.role[0] in 'aeiou' else 'a'} {spec.role} model "
                f"and can't serve {endpoint} requests",
                code="model_not_supported",
            )
        prompt_tokens = estimate(body)
        if spec.context_length and prompt_tokens > spec.context_length:
            # Ollama would silently cut the start of the prompt (the system prompt) to fit.
            raise invalid_request(
                f"The prompt is about {prompt_tokens} tokens, more than the {spec.context_length}-token context "
                f"of '{spec.alias}'. Shorten the messages.",
                code="context_length_exceeded",
            )
        return body, raw, spec, prompt_tokens
    except ForgeError as err:
        await llm.record_rejection(principal, endpoint=endpoint, spec=spec, err=err)
        raise


def _headers(request: Request, record: CallRecord, spec: ModelSpec) -> dict[str, str]:
    return {
        "x-request-id": request.state.request_id,
        "x-forge-queue-ms": str(record.queue_ms),
        "x-forge-model": spec.name,
    }


async def _sse(chunks: AsyncGenerator[dict[str, Any], None], alias: str, *, include_usage: bool) -> AsyncIterator[str]:
    """Re-emit Ollama's chunks as SSE with the model name mapped back to the alias.

    Forge always asks Ollama for the usage chunk (for metering); clients that did not ask for it
    don't get it, like with OpenAI, because naive clients read `choices[0]` of every chunk.
    """
    try:
        async for chunk in chunks:
            if not include_usage and "usage" in chunk:
                if not chunk.get("choices"):
                    continue
                chunk.pop("usage")
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
    body: _Body,
    prompt_estimate: int,
) -> Response:
    upstream["model"] = spec.name
    stream = isinstance(body, _Generation) and body.stream
    # Until the response starts, nothing else notices a client that hangs up: without this, a
    # request abandoned in the GPU queue would keep its ticket and later generate for nobody.
    if stream:
        assert isinstance(body, _Generation)
        # Ask Ollama for a final usage chunk so streamed requests are metered exactly.
        upstream["stream_options"] = {**(upstream.get("stream_options") or {}), "include_usage": True}
        record, chunks = await cancel_on_disconnect(
            request.receive,
            llm.open_stream(
                principal,
                endpoint=endpoint,
                spec=spec,
                priority=priority,
                path=path,
                body=upstream,
                prompt_estimate=prompt_estimate,
            ),
        )
        headers = _headers(request, record, spec) | {"cache-control": "no-cache", "x-accel-buffering": "no"}
        return StreamingResponse(
            _sse(chunks, spec.alias, include_usage=body.wants_usage), media_type="text/event-stream", headers=headers
        )

    data, record = await cancel_on_disconnect(
        request.receive,
        llm.request_json(
            principal,
            endpoint=endpoint,
            spec=spec,
            priority=priority,
            path=path,
            body=upstream,
            prompt_estimate=prompt_estimate,
        ),
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
    body, raw, spec, prompt_estimate = await _prepare(
        request,
        ChatBody,
        endpoint="chat",
        roles=_GENERATIVE,
        estimate=lambda b: sum(estimate_tokens(_message_text(m)) for m in b.messages),
        principal=principal,
        llm=llm,
    )
    return await _proxy(
        request,
        llm,
        principal,
        endpoint="chat",
        spec=spec,
        priority=Priority.CHAT,
        path="/v1/chat/completions",
        upstream=raw,
        body=body,
        prompt_estimate=prompt_estimate,
    )


@router.post("/completions")
async def completions(
    request: Request, principal: Principal = Depends(require_principal), llm: LLM = Depends(get_llm)
) -> Response:
    body, raw, spec, prompt_estimate = await _prepare(
        request,
        CompletionBody,
        endpoint="completions",
        roles=_GENERATIVE,
        estimate=lambda b: estimate_tokens(b.prompt) + estimate_tokens(b.suffix or ""),
        principal=principal,
        llm=llm,
    )
    raw["prompt"] = body.prompt  # a one-element list, unwrapped
    # Autocomplete models get the top priority (and supersession); anything else waits like chat.
    priority = Priority.AUTOCOMPLETE if spec.role == "autocomplete" else Priority.CHAT
    return await _proxy(
        request,
        llm,
        principal,
        endpoint="completions",
        spec=spec,
        priority=priority,
        path="/v1/completions",
        upstream=raw,
        body=body,
        prompt_estimate=prompt_estimate,
    )


@router.post("/embeddings")
async def embeddings(
    request: Request, principal: Principal = Depends(require_principal), llm: LLM = Depends(get_llm)
) -> Response:
    body, raw, spec, prompt_estimate = await _prepare(
        request,
        EmbeddingBody,
        endpoint="embeddings",
        roles=_EMBEDDING,
        estimate=lambda b: sum(estimate_tokens(t) for t in b.texts),
        principal=principal,
        llm=llm,
    )
    return await _proxy(
        request,
        llm,
        principal,
        endpoint="embeddings",
        spec=spec,
        priority=Priority.BATCH,
        path="/v1/embeddings",
        upstream=raw,
        body=body,
        prompt_estimate=prompt_estimate,
    )
