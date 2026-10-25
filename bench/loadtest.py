"""Async load test for Forge: mixes autocomplete, chat and embeddings traffic.

It answers two questions:
  * how fast is Forge under load (latency p50/p95, time to first token, tokens/s,
    how many requests were refused with 429), and
  * does the priority queue work: autocomplete (priority 0) must stay fast while
    chat (priority 1) floods the GPU.

Scenarios
  mixed     every worker type runs at the same time for --duration seconds.
  priority  phase 1: autocomplete alone (baseline); phase 2: the same autocomplete
            load while --chat workers flood the queue. Compares autocomplete p95.

Usage
  pip install -r bench/requirements.txt
  $env:FORGE_API_KEY = "fk_..."
  python bench/loadtest.py --scenario mixed --duration 60 --chat 2 --autocomplete 1 --embed 1
  python bench/loadtest.py --scenario priority --duration 40 --chat 6

A single API key is limited to its rpm_limit (default 120/min); raise it on the API
keys page for heavy runs, otherwise many requests come back 429 rate_limit_exceeded.
More than one autocomplete worker on the same key makes them supersede each other
(Forge keeps only a key's newest queued autocomplete request); those show up as
"superseded", which is the intended behaviour, not an error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "http://forge.localhost:8082/v1"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

# Short, realistic editor states: (code before the cursor, code after it).
FIM_SNIPPETS = [
    ("def is_prime(n: int) -> bool:\n    ", "\n\nprint(is_prime(7))\n"),
    ("import json\n\ndef load_config(path):\n    with open(path) as f:\n        ", "\n    return config\n"),
    ("function debounce(fn, ms) {\n  let timer;\n  return (...args) => {\n    ", "\n  };\n}\n"),
    ("class Stack:\n    def __init__(self):\n        self.items = []\n\n    def push(self, item):\n        ", "\n"),
    ("SELECT customer_id, SUM(amount)\nFROM orders\n", "\nORDER BY 2 DESC;\n"),
]
CHAT_PROMPTS = [
    "Explain the difference between a process and a thread in about 120 words.",
    "Write a Python function that merges two sorted lists, with a docstring.",
    "Review this code and suggest one improvement: for i in range(len(xs)): print(xs[i])",
    "What does HTTP status 429 mean and how should a client react to it?",
]
EMBED_TEXTS = [
    "def add(a, b): return a + b",
    "Kubernetes schedules pods onto nodes.",
    "The GPU queue gives autocomplete the highest priority.",
    "SELECT * FROM usage_events WHERE status >= 400;",
]


@dataclass
class Result:
    kind: str  # autocomplete | chat | embed
    phase: str
    status: int  # HTTP status, 0 = connection error / timeout
    latency_ms: float
    ttft_ms: float | None = None
    completion_tokens: int = 0
    queue_ms: float | None = None  # x-forge-queue-ms: time spent waiting for a GPU slot
    error_type: str | None = None


@dataclass
class Config:
    base_url: str
    api_key: str
    models: dict[str, str]
    timeout_s: float
    embed_batch: int
    chat_max_tokens: int
    autocomplete_max_tokens: int


@dataclass
class Recorder:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)


def _queue_ms(response: httpx.Response) -> float | None:
    value = response.headers.get("x-forge-queue-ms")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _error_type(body: bytes) -> str | None:
    try:
        return json.loads(body)["error"]["type"]
    except (ValueError, KeyError, TypeError):
        return None


async def _stream(client: httpx.AsyncClient, cfg: Config, kind: str, phase: str, path: str, body: dict) -> Result:
    """POST a streaming request and time it: TTFT = first chunk that carries text."""
    started = time.perf_counter()
    first_token: float | None = None
    usage_tokens: int | None = None
    text_chunks = 0
    try:
        async with client.stream("POST", f"{cfg.base_url}{path}", json=body) as response:
            if response.status_code != 200:
                payload = await response.aread()
                return Result(
                    kind,
                    phase,
                    response.status_code,
                    _ms_since(started),
                    queue_ms=_queue_ms(response),
                    error_type=_error_type(payload),
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:  # an error after the stream started
                    return Result(
                        kind,
                        phase,
                        200,
                        _ms_since(started),
                        queue_ms=_queue_ms(response),
                        error_type=chunk["error"].get("type", "stream_error"),
                    )
                if chunk.get("usage"):
                    usage_tokens = chunk["usage"].get("completion_tokens")
                for choice in chunk.get("choices") or []:
                    text = choice.get("text") or (choice.get("delta") or {}).get("content")
                    if text:
                        text_chunks += 1
                        if first_token is None:
                            first_token = time.perf_counter()
            ttft = (first_token - started) * 1000 if first_token else None
            # Ollama streams about one token per chunk; prefer the exact usage count.
            tokens = usage_tokens if usage_tokens is not None else text_chunks
            return Result(kind, phase, 200, _ms_since(started), ttft, tokens, _queue_ms(response))
    except httpx.HTTPError as exc:
        return Result(kind, phase, 0, _ms_since(started), error_type=type(exc).__name__)


async def autocomplete_request(client: httpx.AsyncClient, cfg: Config, phase: str) -> Result:
    prefix, suffix = random.choice(FIM_SNIPPETS)
    body = {
        "model": cfg.models["autocomplete"],
        "prompt": prefix,
        "suffix": suffix,
        "max_tokens": cfg.autocomplete_max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    return await _stream(client, cfg, "autocomplete", phase, "/completions", body)


async def chat_request(client: httpx.AsyncClient, cfg: Config, phase: str) -> Result:
    body = {
        "model": cfg.models["chat"],
        "messages": [{"role": "user", "content": random.choice(CHAT_PROMPTS)}],
        "max_tokens": cfg.chat_max_tokens,
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    return await _stream(client, cfg, "chat", phase, "/chat/completions", body)


async def embed_request(client: httpx.AsyncClient, cfg: Config, phase: str) -> Result:
    texts = [random.choice(EMBED_TEXTS) + f" #{i}" for i in range(cfg.embed_batch)]
    started = time.perf_counter()
    try:
        response = await client.post(f"{cfg.base_url}/embeddings", json={"model": cfg.models["embed"], "input": texts})
    except httpx.HTTPError as exc:
        return Result("embed", phase, 0, _ms_since(started), error_type=type(exc).__name__)
    error = None if response.status_code == 200 else _error_type(response.content)
    return Result(
        "embed", phase, response.status_code, _ms_since(started), queue_ms=_queue_ms(response), error_type=error
    )


REQUESTS = {"autocomplete": autocomplete_request, "chat": chat_request, "embed": embed_request}


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def worker(
    kind: str, client: httpx.AsyncClient, cfg: Config, phase: str, deadline: float, recorder: Recorder, think_s: float
) -> None:
    while time.perf_counter() < deadline:
        result = await REQUESTS[kind](client, cfg, phase)
        recorder.add(result)
        if result.status == 429:
            # Back off like a well-behaved client would (Retry-After is in seconds).
            await asyncio.sleep(min(5.0, max(think_s, 1.0)))
        elif think_s:
            # Jitter so workers don't fire in lockstep.
            await asyncio.sleep(think_s * random.uniform(0.5, 1.5))


async def run_phase(
    phase: str, workers: dict[str, int], seconds: float, cfg: Config, recorder: Recorder, think: dict[str, float]
) -> None:
    active = {k: n for k, n in workers.items() if n > 0}
    print(f"-- phase '{phase}': {seconds:.0f}s, workers {active}", flush=True)
    limits = httpx.Limits(max_connections=sum(workers.values()) + 4)
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    async with httpx.AsyncClient(headers=headers, timeout=cfg.timeout_s, limits=limits) as client:
        deadline = time.perf_counter() + seconds
        tasks = [
            asyncio.create_task(worker(kind, client, cfg, phase, deadline, recorder, think[kind]))
            for kind, count in active.items()
            for _ in range(count)
        ]
        await asyncio.gather(*tasks)


async def resolve_models(cfg: Config) -> None:
    """Fill in model aliases by role from GET /v1/models (unless given on the command line)."""
    wanted = {"autocomplete": "autocomplete", "chat": "chat", "embed": "embedding"}
    if all(cfg.models.get(k) for k in wanted):
        return
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {cfg.api_key}"}, timeout=30) as client:
        response = await client.get(f"{cfg.base_url}/models")
        response.raise_for_status()
        listed = response.json()["data"]
    for kind, role in wanted.items():
        if not cfg.models.get(kind):
            match = next((m["id"] for m in listed if m.get("role") == role), None)
            if match is None:
                raise SystemExit(f"no model with role '{role}' in /v1/models; pass --{kind}-model")
            cfg.models[kind] = match


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


def summarize(results: list[Result]) -> dict:
    ok = [r for r in results if r.status == 200 and r.error_type is None]
    latencies = [r.latency_ms for r in ok]
    ttfts = [r.ttft_ms for r in ok if r.ttft_ms is not None]
    queues = [r.queue_ms for r in ok if r.queue_ms is not None]
    rates = [
        r.completion_tokens / ((r.latency_ms - r.ttft_ms) / 1000)
        for r in ok
        if r.ttft_ms is not None and r.completion_tokens > 1 and r.latency_ms > r.ttft_ms
    ]
    by_type: dict[str, int] = {}
    for r in results:
        if r.status != 200 or r.error_type:
            key = r.error_type or f"http_{r.status}"
            by_type[key] = by_type.get(key, 0) + 1
    return {
        "requests": len(results),
        "ok": len(ok),
        "http_429": sum(1 for r in results if r.status == 429),
        "superseded": by_type.get("superseded", 0),
        "errors_by_type": by_type,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "queue_p95_ms": percentile(queues, 95),
        "tokens_per_s_avg": statistics.fmean(rates) if rates else None,
    }


def _fmt(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value:,.0f}{unit}" if value >= 100 else f"{value:.1f}{unit}"


def print_table(summary: dict[str, dict[str, dict]]) -> None:
    header = (
        f"{'phase':<10} {'kind':<13} {'reqs':>5} {'ok':>5} {'429':>4} {'supers':>6} "
        f"{'p50 ms':>8} {'p95 ms':>8} {'ttft50':>7} {'ttft95':>7} {'queue95':>8} {'tok/s':>6}"
    )
    print("\n" + header)
    print("-" * len(header))
    for phase, kinds in summary.items():
        for kind, s in kinds.items():
            print(
                f"{phase:<10} {kind:<13} {s['requests']:>5} {s['ok']:>5} {s['http_429']:>4} {s['superseded']:>6} "
                f"{_fmt(s['latency_p50_ms']):>8} {_fmt(s['latency_p95_ms']):>8} {_fmt(s['ttft_p50_ms']):>7} "
                f"{_fmt(s['ttft_p95_ms']):>7} {_fmt(s['queue_p95_ms']):>8} {_fmt(s['tokens_per_s_avg']):>6}"
            )
            other = {k: v for k, v in s["errors_by_type"].items() if k not in ("superseded",)}
            if other:
                print(f"{'':<10} {'':<13} errors: {other}")


def priority_verdict(summary: dict[str, dict[str, dict]]) -> str | None:
    base = summary.get("baseline", {}).get("autocomplete")
    flood = summary.get("flood", {}).get("autocomplete")
    chat = summary.get("flood", {}).get("chat")
    if not (base and flood and chat) or base["latency_p95_ms"] is None or flood["latency_p95_ms"] is None:
        return None
    return (
        f"Priority check: autocomplete p95 {flood['latency_p95_ms']:.0f} ms while chat floods "
        f"(baseline {base['latency_p95_ms']:.0f} ms, x{flood['latency_p95_ms'] / base['latency_p95_ms']:.1f}); "
        f"autocomplete queue p95 {_fmt(flood['queue_p95_ms'])} ms vs chat queue p95 {_fmt(chat['queue_p95_ms'])} ms."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load-test Forge with mixed autocomplete/chat/embedding traffic.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FORGE_BASE_URL", DEFAULT_BASE_URL),
        help="Forge API base URL (env FORGE_BASE_URL)",
    )
    parser.add_argument("--api-key", default=os.environ.get("FORGE_API_KEY"), help="API key fk_... (env FORGE_API_KEY)")
    parser.add_argument("--scenario", choices=["mixed", "priority"], default="mixed")
    parser.add_argument("--duration", type=float, default=60, help="seconds of load (priority: per phase)")
    parser.add_argument("--chat", type=int, default=2, help="concurrent chat workers")
    parser.add_argument(
        "--autocomplete",
        type=int,
        default=1,
        help="concurrent autocomplete workers (same key: >1 supersede each other)",
    )
    parser.add_argument("--embed", type=int, default=1, help="concurrent embedding workers (mixed only)")
    parser.add_argument("--embed-batch", type=int, default=16, help="texts per embeddings request")
    parser.add_argument("--chat-max-tokens", type=int, default=256)
    parser.add_argument("--autocomplete-max-tokens", type=int, default=48)
    parser.add_argument(
        "--autocomplete-pause",
        type=float,
        default=0.3,
        help="seconds between one autocomplete worker's requests (a typing pause)",
    )
    parser.add_argument("--chat-model", help="override the chat alias (default: by role from /v1/models)")
    parser.add_argument("--autocomplete-model", help="override the autocomplete alias")
    parser.add_argument("--embed-model", help="override the embedding alias")
    parser.add_argument(
        "--timeout", type=float, default=300, help="per-request timeout in seconds (covers a GPU wake-up)"
    )
    parser.add_argument("--no-save", action="store_true", help="do not write results/loadtest-*.json")
    args = parser.parse_args()
    if not args.api_key:
        parser.error("set FORGE_API_KEY or pass --api-key")

    cfg = Config(
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        models={"chat": args.chat_model, "autocomplete": args.autocomplete_model, "embed": args.embed_model},
        timeout_s=args.timeout,
        embed_batch=args.embed_batch,
        chat_max_tokens=args.chat_max_tokens,
        autocomplete_max_tokens=args.autocomplete_max_tokens,
    )
    think = {"autocomplete": args.autocomplete_pause, "chat": 0.0, "embed": 0.0}
    recorder = Recorder()

    async def run() -> None:
        await resolve_models(cfg)
        print(f"Forge {cfg.base_url}  models {cfg.models}")
        if args.scenario == "mixed":
            await run_phase(
                "mixed",
                {"autocomplete": args.autocomplete, "chat": args.chat, "embed": args.embed},
                args.duration,
                cfg,
                recorder,
                think,
            )
        else:
            await run_phase("baseline", {"autocomplete": args.autocomplete}, args.duration, cfg, recorder, think)
            await run_phase(
                "flood", {"autocomplete": args.autocomplete, "chat": args.chat}, args.duration, cfg, recorder, think
            )

    try:
        asyncio.run(run())
    except httpx.HTTPError as exc:
        print(f"cannot talk to Forge at {cfg.base_url}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted - reporting what finished", file=sys.stderr)

    phases = list(dict.fromkeys(r.phase for r in recorder.results))
    summary = {
        phase: {
            kind: summarize([r for r in recorder.results if r.phase == phase and r.kind == kind])
            for kind in REQUESTS
            if any(r.phase == phase and r.kind == kind for r in recorder.results)
        }
        for phase in phases
    }
    print_table(summary)
    verdict = priority_verdict(summary)
    if verdict:
        print("\n" + verdict)

    if not args.no_save:
        RESULTS_DIR.mkdir(exist_ok=True)
        out = RESULTS_DIR / f"loadtest-{args.scenario}-{datetime.now():%Y%m%d-%H%M%S}.json"
        report = {
            "scenario": args.scenario,
            "base_url": cfg.base_url,
            "models": cfg.models,
            "settings": {k: v for k, v in vars(args).items() if k != "api_key"},
            "summary": summary,
            "priority_verdict": verdict,
            "requests": [asdict(r) for r in recorder.results],
        }
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
