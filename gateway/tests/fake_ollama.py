"""An in-process stand-in for Ollama's HTTP API (and the GPU exporter), served over ASGI.

It implements the endpoints the gateway calls, records every request body, and can be switched
"down" to simulate Ollama at zero replicas (connections are refused, like a missing Service
endpoint).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

EMBED_DIMS = 4


class FakeOllama:
    def __init__(self) -> None:
        self.up = True
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.chat_tokens = ["Hel", "lo", " world"]
        self.completion_text = "return a + b"
        self.loaded: list[dict[str, Any]] = []
        self.supports_usage = True  # False: ignore stream_options like an old Ollama would
        self.error: tuple[int, str] | None = None  # (status, message) for chat to fail with
        # When set, chat/completion handlers wait on it, so tests can hold a GPU slot open.
        self.gate: asyncio.Event | None = None
        self.app = Starlette(
            routes=[
                Route("/api/version", self.version),
                Route("/api/tags", self.tags),
                Route("/api/ps", self.ps),
                Route("/v1/chat/completions", self.chat, methods=["POST"]),
                Route("/v1/completions", self.completions, methods=["POST"]),
                Route("/v1/embeddings", self.embeddings, methods=["POST"]),
                Route("/gpu", self.gpu),
            ]
        )

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body in self.requests if p == path]

    async def _record(self, request: Request) -> dict[str, Any]:
        body = await request.json()
        self.requests.append((request.url.path, body))
        if self.gate is not None:
            await self.gate.wait()
        return body

    async def version(self, _: Request) -> Response:
        return JSONResponse({"version": "0.12.6"})

    async def tags(self, _: Request) -> Response:
        return JSONResponse({"models": [{"name": "qwen2.5-coder:7b"}, {"name": "nomic-embed-text:v1.5"}]})

    async def ps(self, _: Request) -> Response:
        return JSONResponse({"models": self.loaded})

    async def gpu(self, _: Request) -> Response:
        return JSONResponse({"name": "Fake GPU", "utilization_percent": 12, "up": True})

    async def chat(self, request: Request) -> Response:
        body = await self._record(request)
        if self.error is not None:
            status, message = self.error
            return JSONResponse({"error": {"message": message}}, status_code=status)
        usage = {
            "prompt_tokens": 11,
            "completion_tokens": len(self.chat_tokens),
            "total_tokens": 11 + len(self.chat_tokens),
        }
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "".join(self.chat_tokens)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                }
            )

        include_usage = self.supports_usage and (body.get("stream_options") or {}).get("include_usage")

        async def events():
            for token in self.chat_tokens:
                chunk = {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": token}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            if include_usage:
                final = {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "model": body["model"],
                    "choices": [],
                    "usage": usage,
                }
                yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async def completions(self, request: Request) -> Response:
        body = await self._record(request)
        usage = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": "cmpl-1",
                    "object": "text_completion",
                    "model": body["model"],
                    "choices": [{"index": 0, "text": self.completion_text, "finish_reason": "stop"}],
                    "usage": usage,
                }
            )

        async def events():
            for piece in self.completion_text.split(" "):
                chunk = {
                    "id": "cmpl-1",
                    "object": "text_completion",
                    "model": body["model"],
                    "choices": [{"index": 0, "text": piece + " ", "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            if (body.get("stream_options") or {}).get("include_usage"):
                yield f"data: {json.dumps({'id': 'cmpl-1', 'model': body['model'], 'choices': [], 'usage': usage})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async def embeddings(self, request: Request) -> Response:
        body = await self._record(request)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return JSONResponse(
            {
                "object": "list",
                "model": body["model"],
                # Deliberately reversed: the gateway must order embeddings by `index`.
                "data": [
                    {"object": "embedding", "index": i, "embedding": [float(i)] * EMBED_DIMS}
                    for i in reversed(range(len(inputs)))
                ],
                "usage": {"prompt_tokens": 3 * len(inputs), "total_tokens": 3 * len(inputs)},
            }
        )


class SwitchableTransport(httpx.AsyncBaseTransport):
    """Routes the gateway's HTTP client to the fake; refuses connections while it is down."""

    def __init__(self, fake: FakeOllama) -> None:
        self.fake = fake
        self.inner = httpx.ASGITransport(app=fake.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.fake.up and request.url.host != "gpu.test":
            raise httpx.ConnectError("connection refused", request=request)
        return await self.inner.handle_async_request(request)
