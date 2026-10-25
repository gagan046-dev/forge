"""Use Forge from Python with the official OpenAI SDK.

Forge speaks the OpenAI API, so the only Forge-specific parts are the base URL and
an API key created on the "API keys" page of the web console.

    pip install -r integrations/python/requirements.txt
    set FORGE_API_KEY=fk_...            (PowerShell: $env:FORGE_API_KEY = "fk_...")
    python integrations/python/example.py            # runs every demo
    python integrations/python/example.py stream     # just one

Demos: models, chat, stream, fim (fill-in-the-middle code completion), embed.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

from openai import APIConnectionError, APIStatusError, OpenAI

DEFAULT_BASE_URL = "http://forge.localhost:8082/v1"


def demo_models(client: OpenAI) -> None:
    print("Models served by Forge:")
    for model in client.models.list():
        # "role" is a Forge extension field: chat | autocomplete | embedding
        role = (model.model_extra or {}).get("role", "?")
        print(f"  {model.id:<22} role={role}")


def demo_chat(client: OpenAI) -> None:
    # with_raw_response exposes the HTTP headers, including Forge's queue timing.
    raw = client.chat.completions.with_raw_response.create(
        model="forge-chat",
        messages=[
            {"role": "system", "content": "You are a concise senior Python reviewer."},
            {"role": "user", "content": "One sentence: why prefer pathlib over os.path?"},
        ],
        max_tokens=120,
        temperature=0.2,
    )
    completion = raw.parse()
    print(completion.choices[0].message.content.strip())
    usage = completion.usage
    print(
        f"  [{usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens, "
        f"queued {raw.headers.get('x-forge-queue-ms', '?')} ms, backend model {raw.headers.get('x-forge-model')}]"
    )


def demo_stream(client: OpenAI) -> None:
    started = time.perf_counter()
    first_token_at = None
    usage = None
    stream = client.chat.completions.create(
        model="forge-chat",
        messages=[{"role": "user", "content": "Write a Python function that checks if a string is a palindrome."}],
        max_tokens=300,
        stream=True,
        stream_options={"include_usage": True},  # the last chunk then carries token counts
    )
    for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        if not chunk.choices:
            continue
        text = chunk.choices[0].delta.content or ""
        if text and first_token_at is None:
            first_token_at = time.perf_counter()
        print(text, end="", flush=True)
    finished = time.perf_counter()
    print()
    if first_token_at is not None:
        line = f"  [time to first token {1000 * (first_token_at - started):.0f} ms"
        if usage and finished > first_token_at:
            line += f", {usage.completion_tokens / (finished - first_token_at):.1f} tokens/s"
        print(line + "]")


def demo_fim(client: OpenAI) -> None:
    # Fill-in-the-middle: the model sees the code before AND after the gap and
    # writes only the middle. This is what editor autocomplete does on every pause.
    prefix = "def fibonacci(n: int) -> int:\n    "
    suffix = "\n\nprint(fibonacci(10))\n"
    completion = client.completions.create(
        model="forge-autocomplete",
        prompt=prefix,
        suffix=suffix,
        max_tokens=64,
        temperature=0,
    )
    print("--- code before the cursor ---\n" + prefix)
    print("--- the model's fill-in ---\n" + completion.choices[0].text)
    print("--- code after the cursor ---" + suffix, end="")


def demo_embed(client: OpenAI) -> None:
    texts = [
        "How do I read a file line by line in Python?",
        "Iterate over the lines of a text file with Python.",
        "The best pizza in Naples is margherita.",
    ]
    response = client.embeddings.create(model="forge-embed", input=texts)
    vectors = [item.embedding for item in response.data]
    print(f"{len(vectors)} vectors of {len(vectors[0])} dimensions")
    for i in (1, 2):
        print(f"  similarity(text 0, text {i}) = {cosine(vectors[0], vectors[i]):.3f}   {texts[i]!r}")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


DEMOS = {
    "models": demo_models,
    "chat": demo_chat,
    "stream": demo_stream,
    "fim": demo_fim,
    "embed": demo_embed,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Call Forge through the OpenAI Python SDK.")
    parser.add_argument("demo", nargs="?", default="all", choices=["all", *DEMOS], help="which demo to run")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FORGE_BASE_URL", DEFAULT_BASE_URL),
        help=f"Forge API base URL (env FORGE_BASE_URL, default {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FORGE_API_KEY"),
        help="Forge API key fk_... (env FORGE_API_KEY; prefer the env var so the key stays out of shell history)",
    )
    args = parser.parse_args()
    if not args.api_key:
        parser.error("set FORGE_API_KEY (create a key on the API keys page of the Forge web console)")

    # Generous timeout: the first request after an idle period wakes the GPU backend.
    client = OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=300, max_retries=2)
    names = list(DEMOS) if args.demo == "all" else [args.demo]
    try:
        for name in names:
            print(f"\n=== {name} ===")
            DEMOS[name](client)
    except APIConnectionError as exc:
        print(f"cannot reach Forge at {args.base_url}: {exc}", file=sys.stderr)
        print("Is it running, and does Windows resolve forge.localhost (scripts\\add-hosts.ps1)?", file=sys.stderr)
        return 1
    except APIStatusError as exc:
        retry = exc.response.headers.get("retry-after")
        hint = f" (retry after {retry} s)" if retry else ""
        print(f"Forge answered HTTP {exc.status_code}: {exc.message}{hint}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
