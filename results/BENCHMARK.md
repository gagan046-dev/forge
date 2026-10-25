# Forge: Benchmark and Verification Results

All numbers were **measured on this machine** on 2026-10-25: Windows 11, Docker Desktop (WSL2), k3d, **NVIDIA RTX 4070 Laptop GPU, 8 GB VRAM**, Ollama 0.40.2, KV cache q8_0, flash attention on, `OLLAMA_NUM_PARALLEL=2`, `FORGE_MAX_INFLIGHT=2`.

Tools: `bench\bench.py` (per-model speed and VRAM), `bench\loadtest.py` (mixed and priority scenarios). See [docs/COMMANDS.md](../docs/COMMANDS.md) to reproduce.

---

## 1. VRAM

All three models stay resident, 100% on the GPU.

| Model | Role | VRAM |
|---|---|---|
| qwen2.5-coder:7b | chat | 5.02 GB |
| qwen2.5-coder:1.5b-base | autocomplete | 1.30 GB |
| nomic-embed-text:v1.5 | embedding | 0.32 GB |
| **Total used (whole GPU, incl. Windows)** | | **about 7.6-8.1 GB** |

It fits only with `OLLAMA_KV_CACHE_TYPE=q8_0` and flash attention.

## 2. Single-model speed (one request at a time)

| Measurement | Result |
|---|---|
| Chat `forge-chat` generation | 49 tok/s |
| Chat time to first token | p50 about 52 ms, p95 109 ms |
| Chat, 300-token answers | 45-49 tok/s |
| Autocomplete `forge-autocomplete` latency | p50 270-365 ms, p95 505-754 ms |
| Autocomplete generation | about 118-125 tok/s |
| Embeddings, warm | 0.30-0.37 s |

### Autocomplete model choice

| FIM model | p50 | p95 | Quality |
|---|---|---|---|
| qwen2.5-coder:1.5b-base | 270-365 ms | 505-754 ms | clean fills |
| qwen2.5-coder:7b (instruct) | 1583 ms | 2728 ms | worse fills, often chat-style prose |

Decision: the 1.5B **base** model.

## 3. Priority queue under load (chat flood)

Scenario: 6 concurrent chat streams (256 tokens each) plus one autocomplete request every 1.5 s.

| | Before reserved slot | After reserved slot |
|---|---|---|
| Autocomplete p95 latency | 4297 ms | 2146 ms |
| Autocomplete queue wait p95 | 4070 ms | 1 ms |

The reserved slot (one of two GPU slots is held back from chat/batch while autocomplete traffic is seen in the last 30 s) removed the queueing delay. The remaining latency is **GPU compute sharing**: while the 7B generates, the 1.5B model drops from about 125 to 27 tok/s. A scheduler cannot remove that.

## 4. Rate limiting across replicas

Prod runs 2 gateway replicas. A burst of 10 requests against a key with `rpm_limit = 3`:

| Result | Count |
|---|---|
| 200 OK | 3 |
| 429 rate_limit_exceeded | 7 |

The limit is exact because counters live in Redis, not in each pod.

## 5. Scale to zero

| Step | Measured |
|---|---|
| Demand reaches 0 after last request | 600 s |
| Ollama at 0 replicas | about 60 s later (KEDA cooldown) |
| GPU memory used | 7648 MiB to 811 MiB (6.8 GB freed) |
| Wake: backend state `sleeping` to `waking` | 1.1 s |
| Wake: Ollama ready | 9.4 s |
| Wake: first token of the waiting request | 15.3 s |

## 6. RAG (Ask my repo)

| Repository | Files | Chunks | Time |
|---|---|---|---|
| pallets/click (warm models) | 166 | 871 | 13.2 s |
| itsdangerous (cold, models loading) | 44 | not recorded | 26.8 s |

## 7. QA

7 parallel testers plus 2 exclusive runs against the live cluster.

| Round | Result |
|---|---|
| First round | 137 of 145 checks passed |
| Findings fixed | 33 from the round, plus 2 more (reserved slot; monitoring NetworkPolicy) |
| Retest | 40 of 41 passed |
| Remaining | 1 retrieval question: precision limit of the small embedding model, not a bug |

Notable fixes: abandoned autocomplete no longer runs on the GPU (disconnect cancellation), unknown model names no longer create Prometheus labels, over-long prompts are rejected instead of silently truncated, re-index claim shared across replicas through Redis, mobile layout.

## 8. Takeaways

- An 8 GB laptop GPU can serve a 7B chat model, a fast FIM model and embeddings at once, if the KV cache is quantized.
- Interactive latency is dominated by **what else is on the GPU**, not by the model alone. Scheduling (priority plus a reserved slot) helped queueing; it cannot beat shared compute.
- Scale-to-zero is cheap to use: about 15 s to wake and 6.8 GB of VRAM back for the rest of the laptop.
