"""The one path every model call takes: limits -> wake-up -> GPU slot -> Ollama -> metering.

Both the /v1 routes and the RAG module use it, so a RAG question is queued, woken up and metered
exactly like an API request. The public, stable surface for other modules is `LLM.embed`,
`LLM.stream_chat`, `LLM.alias_for_role` and `get_llm` (SPEC 2.4).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, cast

import httpx
from fastapi import Request

from app.asyncutil import run_shielded
from app.auth import Principal
from app.backend import BackendManager
from app.config import ModelSpec, Settings
from app.errors import ForgeError, invalid_request
from app.metering import CallRecord, Metering, estimate_tokens
from app.scheduler import GpuScheduler, Priority

log = logging.getLogger(__name__)

UNKNOWN_MODEL = "unknown"
EmbedInput = Literal["document", "query"]


def _upstream_message(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:500] or "empty response"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or data)[:500]


def chunk_text(chunk: dict[str, Any]) -> str:
    """Generated text in one OpenAI stream chunk (chat delta or completion text)."""
    parts: list[str] = []
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            parts.append(delta["content"])
        elif isinstance(choice.get("text"), str):
            parts.append(choice["text"])
    return "".join(parts)


def response_text(data: dict[str, Any]) -> str:
    """Generated text in a non-streamed chat or completion response."""
    parts: list[str] = []
    for choice in data.get("choices") or []:
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            parts.append(message["content"])
        elif isinstance(choice.get("text"), str):
            parts.append(choice["text"])
    return "".join(parts)


def apply_usage(record: CallRecord, usage: Any) -> bool:
    """Copy an OpenAI `usage` object into the record; False if there was none."""
    if not isinstance(usage, dict):
        return False
    record.prompt_tokens = int(usage.get("prompt_tokens") or 0)
    record.completion_tokens = int(usage.get("completion_tokens") or 0)
    return True


class LLM:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        scheduler: GpuScheduler,
        backend: BackendManager,
        metering: Metering,
    ) -> None:
        self.settings = settings
        self.http = http
        self.scheduler = scheduler
        self.backend = backend
        self.metering = metering
        self.models = settings.models
        self._by_key: dict[str, ModelSpec] = {}
        for spec in self.models:
            self._by_key.setdefault(spec.name, spec)
        for spec in self.models:
            self._by_key[spec.alias] = spec  # aliases win if a name collides with an alias

    # ---- model lookup ----------------------------------------------------------------------

    def lookup(self, model: str) -> ModelSpec | None:
        """A configured model by alias or by its Ollama name, or None."""
        return self._by_key.get(model)

    def resolve(self, model: str) -> ModelSpec:
        """Find a configured model by alias or by its Ollama name; 400 model_not_found if unknown."""
        spec = self.lookup(model)
        if spec is None:
            known = ", ".join(m.alias for m in self.models)
            raise invalid_request(f"Unknown model '{model}'. Available: {known}", code="model_not_found")
        return spec

    def alias_for_role(self, role: str) -> str:
        for spec in self.models:
            if spec.role == role:
                return spec.alias
        raise LookupError(f"No model with role '{role}' is configured")

    # ---- the metered call ------------------------------------------------------------------

    @asynccontextmanager
    async def call(
        self, principal: Principal, *, endpoint: str, spec: ModelSpec, priority: Priority
    ) -> AsyncIterator[CallRecord]:
        """Hold a GPU slot for one upstream call; always writes exactly one usage event."""
        record = CallRecord(endpoint=endpoint, model=spec.alias)
        try:
            await self.metering.check_limits(principal)
            await self.backend.ensure_ready()
            async with self.scheduler.slot(priority, principal.key_id, endpoint) as slot:
                record.queue_ms = slot.queue_ms
                yield record
        except ForgeError as err:
            record.fail(err.status, err.type)
            raise
        except httpx.TimeoutException as exc:
            record.fail(504, "upstream_timeout")
            raise ForgeError(504, "upstream_timeout", "The model server timed out") from exc
        except httpx.TransportError as exc:
            self.backend.mark_down()
            record.fail(503, "backend_unavailable")
            raise ForgeError(
                503, "backend_unavailable", "Lost the connection to the model server", retry_after=10
            ) from exc
        except (asyncio.CancelledError, GeneratorExit):
            if record.status < 400:
                record.fail(499, "client_disconnected")
            raise
        except Exception:
            record.fail(500, "server_error")
            raise
        finally:
            await run_shielded(self._finish(principal, record), what="recording usage")

    async def _finish(self, principal: Principal, record: CallRecord) -> None:
        await self.metering.record(principal, record)
        await self.backend.touch()  # idle time counts from the end of the last request

    async def record_rejection(
        self, principal: Principal, *, endpoint: str, spec: ModelSpec | None, err: ForgeError
    ) -> None:
        """Usage event for a request refused before it reached the queue (bad model, bad body).

        Models that don't resolve are all recorded as "unknown": the request's model string is
        client input, and using it as a metric label or usage row would let any key holder create
        unlimited Prometheus series by sending random names.
        """
        record = CallRecord(endpoint=endpoint, model=spec.alias if spec else UNKNOWN_MODEL)
        record.fail(err.status, err.type)
        await self.metering.record(principal, record)

    async def _raise_for_upstream(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = _upstream_message(await response.aread())
        if response.status_code < 500:
            raise ForgeError(response.status_code, "invalid_request_error", message, code="upstream_rejected")
        raise ForgeError(502, "upstream_error", f"Model server error: {message}")

    def _url(self, path: str) -> str:
        return f"{self.settings.ollama_base}{path}"

    async def request_json(
        self,
        principal: Principal,
        *,
        endpoint: str,
        spec: ModelSpec,
        priority: Priority,
        path: str,
        body: dict[str, Any],
        prompt_estimate: int = 0,
    ) -> tuple[dict[str, Any], CallRecord]:
        """Non-streamed call; returns Ollama's JSON body and the (already metered) record."""
        async with self.call(principal, endpoint=endpoint, spec=spec, priority=priority) as record:
            response = await self.http.post(self._url(path), json=body)
            await self._raise_for_upstream(response)
            data = response.json()
            if not apply_usage(record, data.get("usage")):
                record.prompt_tokens = prompt_estimate
                record.completion_tokens = estimate_tokens(response_text(data))
            return data, record

    async def open_stream(
        self,
        principal: Principal,
        *,
        endpoint: str,
        spec: ModelSpec,
        priority: Priority,
        path: str,
        body: dict[str, Any],
        prompt_estimate: int = 0,
    ) -> tuple[CallRecord, AsyncGenerator[dict[str, Any], None]]:
        """Start a streamed call and return once Ollama has accepted it.

        Everything that can fail with a proper HTTP status (limits, wake-up, queue, upstream
        rejection) happens before this returns, so the caller can still send an error response.
        The returned generator yields parsed SSE chunks and holds the GPU slot until it is
        exhausted or closed; callers must close it (async with aclosing / try-finally).
        """
        events = self._stream(principal, endpoint, spec, priority, path, body, prompt_estimate)
        record = cast(CallRecord, await anext(events))
        return record, cast(AsyncGenerator[dict[str, Any], None], events)

    async def _stream(
        self,
        principal: Principal,
        endpoint: str,
        spec: ModelSpec,
        priority: Priority,
        path: str,
        body: dict[str, Any],
        prompt_estimate: int,
    ) -> AsyncGenerator[CallRecord | dict[str, Any], None]:
        async with self.call(principal, endpoint=endpoint, spec=spec, priority=priority) as record:
            accepted = got_usage = False
            generated: list[str] = []
            try:
                async with self.http.stream("POST", self._url(path), json=body) as response:
                    await self._raise_for_upstream(response)
                    accepted = True
                    yield record  # the caller may start its streaming response now
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            log.warning("skipping malformed stream line from the model server")
                            continue
                        text = chunk_text(chunk)
                        if text:
                            record.mark_first_token()
                            generated.append(text)
                        if apply_usage(record, chunk.get("usage")):
                            got_usage = True
                        yield chunk
            finally:
                if accepted and not got_usage:
                    # No usage chunk (old upstream, or the client left mid-answer): estimate, so
                    # the tokens the GPU did generate still count against the quota.
                    record.prompt_tokens = prompt_estimate
                    record.completion_tokens = estimate_tokens("".join(generated))

    # ---- stable internal API (used by app.rag) ---------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        principal: Principal,
        priority: Priority = Priority.BATCH,
        input_type: EmbedInput | None = None,
    ) -> list[list[float]]:
        """Embed texts with the embedding model.

        `input_type` adds the model's task prefix from models.yaml (documentPrefix/queryPrefix):
        retrieval models like nomic-embed-text are trained to see "search_document: " on indexed
        text and "search_query: " on questions, and match noticeably worse without them.
        """
        if not texts:
            return []
        spec = self.resolve(self.alias_for_role("embedding"))
        prefix = {"document": spec.document_prefix, "query": spec.query_prefix}.get(input_type or "", "")
        data, _ = await self.request_json(
            principal,
            endpoint="rag",
            spec=spec,
            priority=priority,
            path="/v1/embeddings",
            body={"model": spec.name, "input": [prefix + t for t in texts]},
            prompt_estimate=sum(estimate_tokens(t) for t in texts),
        )
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        if len(items) != len(texts):
            raise ForgeError(502, "upstream_error", f"Expected {len(texts)} embeddings, got {len(items)}")
        return [item["embedding"] for item in items]

    def stream_chat(
        self,
        messages: list[dict],
        *,
        model_alias: str,
        principal: Principal,
        priority: Priority = Priority.CHAT,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> ChatStream:
        """Iterate the answer's text deltas. Raises ForgeError subclasses like the /v1 routes.

        The returned stream's `usage` holds the token counts once it has been read to the end.
        """
        spec = self.resolve(model_alias)
        body: dict[str, Any] = {
            "model": spec.name,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return ChatStream(
            lambda: self.open_stream(
                principal,
                endpoint="rag",
                spec=spec,
                priority=priority,
                path="/v1/chat/completions",
                body=body,
                prompt_estimate=sum(estimate_tokens(str(m.get("content") or "")) for m in messages),
            )
        )


_OpenStream = tuple[CallRecord, AsyncGenerator[dict[str, Any], None]]


class ChatStream:
    """Async iterator of text deltas from one streamed chat call (see LLM.stream_chat)."""

    def __init__(self, open_stream: Callable[[], Awaitable[_OpenStream]]) -> None:
        self.usage: dict[str, int] | None = None
        # Opened on first iteration, so a stream that is never read never queues for the GPU.
        self._deltas = self._run(open_stream)

    def __aiter__(self) -> ChatStream:
        return self

    async def __anext__(self) -> str:
        return await anext(self._deltas)

    async def aclose(self) -> None:
        await self._deltas.aclose()

    async def _run(self, open_stream: Callable[[], Awaitable[_OpenStream]]) -> AsyncGenerator[str, None]:
        record, chunks = await open_stream()
        try:
            async for chunk in chunks:
                text = chunk_text(chunk)
                if text:
                    yield text
        finally:
            await chunks.aclose()  # releases the GPU slot and writes the usage event
        # Only reached when the answer finished; the counts are Ollama's (or estimated).
        self.usage = {
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.prompt_tokens + record.completion_tokens,
        }


def get_llm(request: Request) -> LLM:
    """FastAPI dependency."""
    return request.app.state.llm
