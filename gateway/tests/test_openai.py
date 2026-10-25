"""The /v1 proxy: SSE pass-through, metering, FIM completions, embeddings, errors and headers."""

from __future__ import annotations

import asyncio
import json

from conftest import usage_events

from app.auth import Principal
from app.scheduler import Priority

CHAT = {"model": "forge-chat", "messages": [{"role": "user", "content": "Say hello"}]}


def sse_data(text: str) -> list[str]:
    return [line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")]


def console_principal(gateway) -> Principal:
    return Principal(key_id=gateway.state.web_console_key_id, name="web-console", is_admin=True)


async def test_streaming_chat_passes_chunks_through_and_meters_usage(gateway, bearer, database_url):
    key = await gateway.create_key()
    response = await gateway.client.post("/v1/chat/completions", json=CHAT | {"stream": True}, headers=bearer(key))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-forge-model"] == "qwen2.5-coder:7b"
    assert int(response.headers["x-forge-queue-ms"]) >= 0
    assert response.headers["x-request-id"]

    data = sse_data(response.text)
    assert data[-1] == "[DONE]"
    chunks = [json.loads(d) for d in data[:-1]]
    assert "".join(c["choices"][0]["delta"]["content"] for c in chunks if c["choices"]) == "Hello world"
    assert {c["model"] for c in chunks} == {"forge-chat"}  # mapped back to the alias
    assert chunks[-1]["usage"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}

    upstream = gateway.ollama.bodies("/v1/chat/completions")[-1]
    assert upstream["model"] == "qwen2.5-coder:7b"
    assert upstream["stream_options"] == {"include_usage": True}

    [event] = await usage_events(database_url)
    assert event["key_id"] == key["id"]
    assert (event["endpoint"], event["model"], event["status"]) == ("chat", "forge-chat", 200)
    assert (event["prompt_tokens"], event["completion_tokens"]) == (11, 3)
    assert event["ttft_ms"] is not None
    assert event["duration_ms"] >= event["ttft_ms"]
    assert event["error_type"] is None

    assert await gateway.state.metering.tokens_today([key["id"]]) == {key["id"]: 14}


async def test_stream_without_usage_chunk_is_estimated(gateway, bearer, database_url):
    key = await gateway.create_key()
    gateway.ollama.supports_usage = False
    response = await gateway.client.post("/v1/chat/completions", json=CHAT | {"stream": True}, headers=bearer(key))
    assert response.status_code == 200
    [event] = await usage_events(database_url)
    assert event["prompt_tokens"] > 0
    assert event["completion_tokens"] > 0


async def test_non_streaming_chat_rewrites_model_and_meters(gateway, bearer, database_url):
    key = await gateway.create_key()
    response = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "forge-chat"
    assert body["choices"][0]["message"]["content"] == "Hello world"
    [event] = await usage_events(database_url)
    assert (event["prompt_tokens"], event["completion_tokens"], event["status"]) == (11, 3, 200)
    assert event["ttft_ms"] is None
    assert event["tokens_per_s"] is not None


async def test_raw_ollama_names_are_accepted(gateway, bearer):
    key = await gateway.create_key()
    response = await gateway.client.post(
        "/v1/chat/completions", json=CHAT | {"model": "qwen2.5-coder:7b"}, headers=bearer(key)
    )
    assert response.status_code == 200
    assert response.json()["model"] == "forge-chat"


async def test_completions_forward_suffix_for_fill_in_the_middle(gateway, bearer, database_url):
    key = await gateway.create_key()
    body = {
        "model": "forge-autocomplete",
        "prompt": "def add(a, b):\n    ",
        "suffix": "\n\nprint(add(1, 2))",
        "max_tokens": 32,
        "temperature": 0.1,
        "stop": ["\n\n"],
    }
    response = await gateway.client.post("/v1/completions", json=body, headers=bearer(key))
    assert response.status_code == 200
    assert response.json()["choices"][0]["text"] == "return a + b"
    assert response.json()["model"] == "forge-autocomplete"

    assert gateway.ollama.bodies("/v1/completions")[-1] == body | {"model": "qwen2.5-coder:1.5b-base"}
    [event] = await usage_events(database_url)
    assert (event["endpoint"], event["model"], event["prompt_tokens"]) == ("completions", "forge-autocomplete", 20)


async def test_streaming_completions(gateway, bearer):
    key = await gateway.create_key()
    response = await gateway.client.post(
        "/v1/completions",
        json={"model": "forge-autocomplete", "prompt": "x = ", "suffix": "\n", "stream": True},
        headers=bearer(key),
    )
    assert response.status_code == 200
    data = sse_data(response.text)
    assert data[-1] == "[DONE]"
    text = "".join(c["text"] for d in data[:-1] for c in json.loads(d)["choices"])
    assert text.strip() == "return a + b"
    assert gateway.ollama.bodies("/v1/completions")[-1]["suffix"] == "\n"


async def test_embeddings_for_string_and_list_input(gateway, bearer, database_url):
    key = await gateway.create_key()
    single = await gateway.client.post(
        "/v1/embeddings", json={"model": "forge-embed", "input": "hello"}, headers=bearer(key)
    )
    assert single.status_code == 200
    assert single.json()["model"] == "forge-embed"
    assert len(single.json()["data"]) == 1

    many = await gateway.client.post(
        "/v1/embeddings", json={"model": "forge-embed", "input": ["a", "b", "c"]}, headers=bearer(key)
    )
    assert many.status_code == 200
    assert len(many.json()["data"]) == 3
    assert gateway.ollama.bodies("/v1/embeddings")[-1] == {"model": "nomic-embed-text:v1.5", "input": ["a", "b", "c"]}

    events = await usage_events(database_url)
    assert [(e["endpoint"], e["prompt_tokens"]) for e in events] == [("embeddings", 3), ("embeddings", 9)]


async def test_internal_llm_embed_orders_by_index(gateway, database_url):
    vectors = await gateway.state.llm.embed(["a", "b", "c"], principal=console_principal(gateway))
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]
    [event] = await usage_events(database_url)
    assert (event["endpoint"], event["model"]) == ("rag", "forge-embed")


async def test_internal_llm_stream_chat_yields_text(gateway, database_url):
    llm = gateway.state.llm
    deltas = [
        d
        async for d in llm.stream_chat(
            CHAT["messages"], model_alias=llm.alias_for_role("chat"), principal=console_principal(gateway)
        )
    ]
    assert "".join(deltas) == "Hello world"
    [event] = await usage_events(database_url)
    assert (event["endpoint"], event["completion_tokens"]) == ("rag", 3)


async def test_unknown_model_is_400_and_recorded(gateway, bearer, database_url):
    key = await gateway.create_key()
    response = await gateway.client.post("/v1/chat/completions", json=CHAT | {"model": "gpt-9"}, headers=bearer(key))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "model_not_found"
    [event] = await usage_events(database_url)
    assert (event["status"], event["model"], event["error_type"]) == (400, "gpt-9", "invalid_request_error")


async def test_bad_bodies_are_openai_shaped_400s(gateway, bearer):
    key = await gateway.create_key()
    no_messages = await gateway.client.post("/v1/chat/completions", json={"model": "forge-chat"}, headers=bearer(key))
    assert no_messages.status_code == 400
    assert "messages" in no_messages.json()["error"]["message"]
    not_json = await gateway.client.post(
        "/v1/embeddings", content=b"{nope", headers=bearer(key) | {"content-type": "application/json"}
    )
    assert not_json.status_code == 400
    assert not_json.json()["error"]["type"] == "invalid_request_error"


async def test_upstream_errors_are_passed_on(gateway, bearer, database_url):
    key = await gateway.create_key()
    gateway.ollama.error = (404, "model 'qwen2.5-coder:7b' not found, try pulling it first")
    rejected = await gateway.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert rejected.status_code == 404
    assert "not found" in rejected.json()["error"]["message"]

    gateway.ollama.error = (500, "CUDA out of memory")
    failed = await gateway.client.post("/v1/chat/completions", json=CHAT | {"stream": True}, headers=bearer(key))
    assert failed.status_code == 502
    assert failed.json()["error"]["type"] == "upstream_error"
    assert [e["status"] for e in await usage_events(database_url)] == [404, 502]
    assert await gateway.state.scheduler.inflight() == 0  # slots were released


async def test_superseded_autocomplete_returns_499(gateway, bearer):
    key = await gateway.create_key()
    scheduler = gateway.state.scheduler
    request = {"model": "forge-autocomplete", "prompt": "a", "suffix": "b"}

    # Occupy both GPU slots so the autocomplete requests have to queue.
    async with scheduler.slot(Priority.CHAT, 999, "holder"), scheduler.slot(Priority.CHAT, 998, "holder"):
        older = asyncio.create_task(gateway.client.post("/v1/completions", json=request, headers=bearer(key)))
        while await scheduler.queue_depth() < 1:
            await asyncio.sleep(0.01)
        newer = asyncio.create_task(gateway.client.post("/v1/completions", json=request, headers=bearer(key)))
        superseded = await asyncio.wait_for(older, 5)
        assert superseded.status_code == 499
        assert superseded.json()["error"]["type"] == "superseded"
    assert (await asyncio.wait_for(newer, 5)).status_code == 200


async def test_abandoned_stream_releases_its_slot_and_is_recorded(gateway, database_url):
    """A client that disconnects mid-stream (the route closes the generator) frees the GPU slot."""
    llm = gateway.state.llm
    spec = llm.resolve("forge-chat")
    _, chunks = await llm.open_stream(
        console_principal(gateway),
        endpoint="chat",
        spec=spec,
        priority=Priority.CHAT,
        path="/v1/chat/completions",
        body=CHAT | {"model": spec.name, "stream": True},
    )
    assert await gateway.state.scheduler.inflight() == 1
    first = await anext(chunks)
    assert first["choices"][0]["delta"]["content"] == "Hel"
    await chunks.aclose()

    assert await gateway.state.scheduler.inflight() == 0
    [event] = await usage_events(database_url)
    assert (event["status"], event["error_type"]) == (499, "client_disconnected")
    assert event["ttft_ms"] is not None
    assert event["completion_tokens"] > 0  # estimated from what was generated before the disconnect
