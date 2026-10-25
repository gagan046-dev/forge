"""Helpers shared by the RAG tests (imported after conftest has set up `app`)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator

from app.auth import Principal

ADMIN = Principal(key_id=1, name="web-console", is_admin=True)


def parse_sse(body: str) -> list[tuple[str, object]]:
    """Parse a text/event-stream body into (event, decoded JSON data) pairs."""
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.split("\n"))
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def text_embedding(text: str, dims: int = 768) -> list[float]:
    """Deterministic bag-of-words embedding: texts sharing words point in similar directions,
    so retrieval in the tests behaves like the real thing, just cruder."""
    vector = [0.0] * dims
    for word in re.findall(r"[a-z0-9_]+", text.lower()):
        bucket = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % dims
        vector[bucket] += 1.0
    if not any(vector):  # pgvector can't compute cosine distance for a zero vector
        vector[0] = 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


class FakeLLM:
    """Implements the SPEC 2.4 LLM interface without a GPU."""

    def __init__(self, answer: list[str] | None = None) -> None:
        self.answer = answer or ["The answer ", "is in ", "the sources [1]."]
        self.embed_calls: list[tuple[int, int]] = []  # (batch size, priority)
        self.embed_input_types: list[str | None] = []
        self.chat_calls: list[dict] = []
        self.fail_embed_after: int | None = None

    async def embed(self, texts, *, principal, priority=2, input_type=None):
        if self.fail_embed_after is not None and len(self.embed_calls) >= self.fail_embed_after:
            raise RuntimeError("embedding backend unavailable")
        self.embed_calls.append((len(texts), int(priority)))
        self.embed_input_types.append(input_type)
        return [text_embedding(t) for t in texts]

    async def stream_chat(
        self, messages, *, model_alias, principal, priority=1, max_tokens=None, temperature=0.2
    ) -> AsyncIterator[str]:
        self.chat_calls.append({"messages": messages, "model_alias": model_alias, "priority": int(priority)})
        for delta in self.answer:
            yield delta

    def alias_for_role(self, role: str) -> str:
        return {"chat": "forge-chat", "embedding": "forge-embed"}[role]
