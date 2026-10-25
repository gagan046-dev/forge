"""Per-model benchmark for Forge: tokens/s, time to first token and VRAM.

For every model in GET /v1/models it does one warm-up request (which loads the
model into VRAM and is not measured), then --runs measured requests:
  chat          streamed chat completion, fixed prompt, temperature 0
  autocomplete  streamed fill-in-the-middle completion (prompt + suffix)
  embedding     one batch of --embed-batch texts
and then records how much VRAM the model takes.

VRAM comes from the gateway's admin API when --admin-password is given (per-model
size_vram from Ollama, plus the whole GPU from the gpu-exporter); otherwise from
nvidia-smi on this machine, which sees the same GPU (whole-GPU number only).

Writes results/bench-<timestamp>.md and .json.

Usage
  pip install -r bench/requirements.txt
  $env:FORGE_API_KEY = "fk_..."
  $env:FORGE_ADMIN_PASSWORD = Get-Content secrets/prod-admin-password.txt   # optional
  python bench/bench.py --runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from functools import partial
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "http://forge.localhost:8082/v1"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

CHAT_PROMPT = "Explain in about 150 words how a hash map handles collisions."
FIM_PREFIX = "def binary_search(items: list[int], target: int) -> int:\n    "
FIM_SUFFIX = "\n\nassert binary_search([1, 3, 5, 7], 5) == 2\n"
EMBED_TEXT = "Forge routes every request through one shared GPU queue with priorities. "


class Busy(RuntimeError):
    """Forge said "not now" (429 rate limit / queue full, 503 backend waking): worth a retry."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


def _check(response: httpx.Response, what: str) -> None:
    if response.status_code in (429, 503):
        retry_after = float(response.headers.get("retry-after") or 5)
        raise Busy(f"{what}: HTTP {response.status_code} {response.text[:200]}", retry_after)
    if response.status_code != 200:
        raise RuntimeError(f"{what}: HTTP {response.status_code} {response.text[:200]}")


def with_retries(call, attempts: int = 5):
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Busy as exc:
            if attempt == attempts:
                raise
            wait = min(exc.retry_after, 30.0)
            print(f"  busy ({exc}); retrying in {wait:.0f}s", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


class Forge:
    def __init__(self, base_url: str, api_key: str, admin_password: str | None, timeout: float):
        self.base_url = base_url.rstrip("/")
        # The admin API lives next to /v1 on the same host.
        self.root_url = self.base_url.removesuffix("/v1")
        self.api = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        self.admin: httpx.Client | None = None
        if admin_password:
            self.admin = httpx.Client(headers={"X-Forge-Csrf": "1"}, timeout=30)
            response = self.admin.post(f"{self.root_url}/api/auth/login", json={"password": admin_password})
            if response.status_code != 200:
                raise SystemExit(f"admin login failed: HTTP {response.status_code}")

    def models(self) -> list[dict]:
        response = self.api.get(f"{self.base_url}/models")
        response.raise_for_status()
        return response.json()["data"]

    def stream(self, path: str, body: dict) -> dict:
        """One streamed request: wall time, time to first token, completion tokens."""
        started = time.perf_counter()
        first: float | None = None
        tokens: int | None = None
        chunks = 0
        with self.api.stream("POST", f"{self.base_url}{path}", json=body) as response:
            if response.status_code != 200:
                response.read()
                _check(response, path)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise RuntimeError(f"{path}: {chunk['error']}")
                if chunk.get("usage"):
                    tokens = chunk["usage"].get("completion_tokens")
                for choice in chunk.get("choices") or []:
                    if choice.get("text") or (choice.get("delta") or {}).get("content"):
                        chunks += 1
                        first = first or time.perf_counter()
        end = time.perf_counter()
        tokens = tokens if tokens is not None else chunks
        ttft = (first - started) if first else None
        generating = (end - first) if first else None
        return {
            "wall_s": end - started,
            "ttft_s": ttft,
            "completion_tokens": tokens,
            # decode speed: tokens after the first one, over the time after the first one
            "tokens_per_s": (tokens - 1) / generating if generating and tokens > 1 else None,
        }

    def embed(self, model: str, batch: int) -> dict:
        texts = [f"{EMBED_TEXT}({i})" for i in range(batch)]
        started = time.perf_counter()
        response = self.api.post(f"{self.base_url}/embeddings", json={"model": model, "input": texts})
        _check(response, "/embeddings")
        wall = time.perf_counter() - started
        data = response.json()
        return {
            "wall_s": wall,
            "texts_per_s": batch / wall,
            "dimensions": len(data["data"][0]["embedding"]),
            "prompt_tokens": (data.get("usage") or {}).get("prompt_tokens"),
        }

    def vram(self, alias: str) -> dict:
        """VRAM taken by this model (admin API) and by the whole GPU (admin API or nvidia-smi)."""
        info: dict = {"model_vram_mib": None, "gpu_used_mib": None, "gpu_total_mib": None, "source": None}
        if self.admin:
            models = self.admin.get(f"{self.root_url}/api/models").json()["models"]
            entry = next((m for m in models if m["alias"] == alias), None)
            if entry and entry.get("size_vram"):
                info["model_vram_mib"] = entry["size_vram"] / 2**20
            gpu = self.admin.get(f"{self.root_url}/api/status").json().get("gpu") or {}
            if gpu.get("memory_used_bytes") is not None:
                info["gpu_used_mib"] = gpu["memory_used_bytes"] / 2**20
                info["gpu_total_mib"] = (gpu.get("memory_total_bytes") or 0) / 2**20 or None
                info["source"] = "forge admin API"
        if info["gpu_used_mib"] is None:
            used, total = nvidia_smi_memory()
            if used is not None:
                info.update(gpu_used_mib=used, gpu_total_mib=total, source="nvidia-smi")
        return info


def nvidia_smi_memory() -> tuple[float | None, float | None]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        used, total = (float(x) for x in out.strip().splitlines()[0].split(","))
        return used, total
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, None


def median(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def bench_model(forge: Forge, model: dict, runs: int, embed_batch: int) -> dict:
    alias, role = model["id"], model.get("role")
    print(f"\n== {alias} ({role}) ==", flush=True)
    if role == "chat":
        body = {
            "model": alias,
            "messages": [{"role": "user", "content": CHAT_PROMPT}],
            "max_tokens": 256,
            "temperature": 0,
            "seed": 7,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        call = partial(forge.stream, "/chat/completions", body)
    elif role == "autocomplete":
        body = {
            "model": alias,
            "prompt": FIM_PREFIX,
            "suffix": FIM_SUFFIX,
            "max_tokens": 96,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        call = partial(forge.stream, "/completions", body)
    elif role == "embedding":
        call = partial(forge.embed, alias, embed_batch)
    else:
        print(f"  skipping unknown role {role!r}")
        return {"alias": alias, "role": role, "skipped": True}

    started = time.perf_counter()
    with_retries(call)  # warm-up: wakes the backend if needed and loads the model into VRAM
    print(f"  warm-up {time.perf_counter() - started:.1f}s (includes model load)")
    samples = []
    for i in range(runs):
        sample = with_retries(call)
        samples.append(sample)
        if role == "embedding":
            print(
                f"  run {i + 1}: {embed_batch} texts in {sample['wall_s'] * 1000:.0f} ms "
                f"({sample['texts_per_s']:.1f} texts/s)"
            )
        else:
            rate = sample["tokens_per_s"]
            ttft = sample["ttft_s"]
            print(
                f"  run {i + 1}: {sample['completion_tokens']} tokens, "
                f"{'-' if rate is None else f'{rate:.1f}'} tok/s, "
                f"TTFT {'-' if ttft is None else f'{ttft * 1000:.0f} ms'}, total {sample['wall_s']:.2f}s"
            )
    vram = forge.vram(alias)
    result = {
        "alias": alias,
        "role": role,
        "runs": samples,
        "vram": vram,
        "median_wall_s": median([s["wall_s"] for s in samples]),
    }
    if role == "embedding":
        result["median_texts_per_s"] = median([s["texts_per_s"] for s in samples])
        result["dimensions"] = samples[0]["dimensions"] if samples else None
    else:
        result["median_tokens_per_s"] = median([s["tokens_per_s"] for s in samples])
        result["median_ttft_ms"] = median([s["ttft_s"] * 1000 if s["ttft_s"] else None for s in samples])
    return result


def _num(value: float | None, fmt: str = ".1f", unit: str = "") -> str:
    return "n/a" if value is None else f"{value:{fmt}}{unit}"


def markdown_report(results: list[dict], args: argparse.Namespace) -> str:
    lines = [
        "# Forge model benchmark",
        "",
        f"- Date: {datetime.now():%Y-%m-%d %H:%M}",
        f"- Endpoint: {args.base_url}",
        f"- Median of {args.runs} runs after one warm-up. Chat: 256 max tokens, temperature 0. "
        f"Autocomplete: FIM with suffix, 96 max tokens. Embeddings: batches of {args.embed_batch} texts.",
        "",
        "| Model | Role | Speed | Time to first token | Wall time | Model VRAM | GPU VRAM used |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.get("skipped"):
            continue
        v = r["vram"]
        speed = (
            f"{_num(r.get('median_texts_per_s'))} texts/s"
            if r["role"] == "embedding"
            else f"{_num(r.get('median_tokens_per_s'))} tok/s"
        )
        ttft = "-" if r["role"] == "embedding" else _num(r.get("median_ttft_ms"), ".0f", " ms")
        gpu = (
            f"{v['gpu_used_mib']:.0f} / {v['gpu_total_mib']:.0f} MiB"
            if v["gpu_used_mib"] and v["gpu_total_mib"]
            else "n/a"
        )
        lines.append(
            f"| {r['alias']} | {r['role']} | {speed} | {ttft} | {_num(r['median_wall_s'], '.2f', ' s')} | "
            f"{_num(v['model_vram_mib'], '.0f', ' MiB')} | {gpu} |"
        )
    sources = sorted({r["vram"]["source"] for r in results if not r.get("skipped") and r["vram"]["source"]})
    lines += [
        "",
        f"VRAM source: {', '.join(sources) or 'unavailable'}. "
        "GPU VRAM is measured right after each model's runs, so it includes models loaded before it.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark every Forge model: tokens/s, TTFT and VRAM; writes results/bench-*.md/json.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FORGE_BASE_URL", DEFAULT_BASE_URL),
        help="Forge API base URL (env FORGE_BASE_URL)",
    )
    parser.add_argument("--api-key", default=os.environ.get("FORGE_API_KEY"), help="API key (env FORGE_API_KEY)")
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("FORGE_ADMIN_PASSWORD"),
        help="web console password for per-model VRAM (env FORGE_ADMIN_PASSWORD; optional)",
    )
    parser.add_argument("--runs", type=int, default=3, help="measured runs per model")
    parser.add_argument("--embed-batch", type=int, default=32, help="texts per embeddings request")
    parser.add_argument("--models", nargs="*", help="only these aliases (default: all from /v1/models)")
    parser.add_argument("--timeout", type=float, default=600, help="per-request timeout, s (covers wake-up)")
    args = parser.parse_args()
    if not args.api_key:
        parser.error("set FORGE_API_KEY or pass --api-key")

    try:
        forge = Forge(args.base_url, args.api_key, args.admin_password, args.timeout)
        models = [m for m in forge.models() if not args.models or m["id"] in args.models]
        if not models:
            raise SystemExit("no models to benchmark")
        results = [bench_model(forge, m, args.runs, args.embed_batch) for m in models]
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    report = markdown_report(results, args)
    md_path = RESULTS_DIR / f"bench-{stamp}.md"
    json_path = RESULTS_DIR / f"bench-{stamp}.json"
    md_path.write_text(report, encoding="utf-8")
    settings = {k: v for k, v in vars(args).items() if k not in ("api_key", "admin_password")}
    json_path.write_text(json.dumps({"settings": settings, "results": results}, indent=2), encoding="utf-8")
    print("\n" + report)
    print(f"Saved {md_path} and {json_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
