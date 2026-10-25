"""Forge GPU exporter: nvidia-smi -> Prometheus metrics and a small JSON API.

Every POLL_SECONDS it runs one nvidia-smi query and keeps the parsed result in memory.
HTTP (port 9400):
  GET /metrics   Prometheus text format (forge_gpu_*)
  GET /gpu       the same data as JSON (used by the gateway's /api/status)
  GET /healthz   200 while the HTTP server runs (the GPU being unreadable is a metric, not a crash)

Standard library only: the image stays tiny and has nothing to patch. It runs with
runtimeClassName: nvidia but WITHOUT an nvidia.com/gpu request, so the only GPU stays
free for Ollama; on WSL2 the NVIDIA runtime still injects /dev/dxg and nvidia-smi.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The exact fields the SPEC asks for, in this order. "nounits" gives plain numbers:
# percent, MiB, MiB, degrees C, W, W.
QUERY_FIELDS = (
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
    "power.limit",
)
MIB = 1024 * 1024

log = logging.getLogger("gpu-exporter")


@dataclass(frozen=True)
class GpuSample:
    index: int
    name: str
    utilization_percent: float | None
    memory_used_bytes: int | None
    memory_total_bytes: int | None
    temperature_celsius: float | None
    power_watts: float | None
    power_limit_watts: float | None


def _number(raw: str) -> float | None:
    """nvidia-smi prints "[N/A]" or "[Not Supported]" for values a GPU can't report
    (laptop GPUs often lack power readings). Those become None, i.e. "no sample"."""
    value = raw.strip()
    if not value or value.startswith("["):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _mib_to_bytes(raw: str) -> int | None:
    mib = _number(raw)
    return None if mib is None else int(mib * MIB)


def parse_csv(output: str) -> list[GpuSample]:
    """Parse `--format=csv,noheader,nounits` output: one line per GPU, in index order."""
    samples = []
    rows = csv.reader(output.splitlines(), skipinitialspace=True)
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(QUERY_FIELDS):
            raise ValueError(f"expected {len(QUERY_FIELDS)} fields, got {len(row)}: {row!r}")
        name, util, mem_used, mem_total, temp, power, power_limit = row
        samples.append(
            GpuSample(
                index=len(samples),
                name=name.strip(),
                utilization_percent=_number(util),
                memory_used_bytes=_mib_to_bytes(mem_used),
                memory_total_bytes=_mib_to_bytes(mem_total),
                temperature_celsius=_number(temp),
                power_watts=_number(power),
                power_limit_watts=_number(power_limit),
            )
        )
    return samples


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


# metric name -> (GpuSample field, HELP text)
_GAUGES = (
    ("forge_gpu_utilization_percent", "utilization_percent", "GPU core utilisation in percent."),
    ("forge_gpu_memory_used_bytes", "memory_used_bytes", "GPU memory (VRAM) in use, bytes."),
    ("forge_gpu_memory_total_bytes", "memory_total_bytes", "Total GPU memory (VRAM), bytes."),
    ("forge_gpu_temperature_celsius", "temperature_celsius", "GPU temperature in degrees Celsius."),
    ("forge_gpu_power_watts", "power_watts", "GPU power draw in watts."),
    ("forge_gpu_power_limit_watts", "power_limit_watts", "GPU power limit in watts."),
)


def render_metrics(samples: list[GpuSample], up: bool) -> str:
    lines = [
        "# HELP forge_gpu_up 1 if the last nvidia-smi query succeeded, else 0.",
        "# TYPE forge_gpu_up gauge",
        f"forge_gpu_up {1 if up else 0}",
    ]
    if not up:
        # Stale numbers would look like a healthy, idle GPU; export nothing instead.
        return "\n".join(lines) + "\n"
    for metric, field, help_text in _GAUGES:
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        for s in samples:
            value = getattr(s, field)
            if value is None:
                continue
            labels = f'gpu="{s.index}",name="{_escape_label(s.name)}"'
            # repr keeps full precision (8 GiB in bytes must not become 8.58993e+09)
            lines.append(f"{metric}{{{labels}}} {value!r}")
    return "\n".join(lines) + "\n"


def render_json(samples: list[GpuSample], up: bool, updated_at: float | None, error: str | None) -> dict:
    """The /gpu document. The first GPU's fields are also at the top level, because
    this machine has exactly one GPU and that keeps clients simple."""
    gpus = [asdict(s) for s in samples] if up else []
    doc: dict = {
        "up": up,
        "updated_at": datetime.fromtimestamp(updated_at, timezone.utc).isoformat() if updated_at else None,
        "error": error,
        "gpus": gpus,
    }
    if gpus:
        doc.update(gpus[0])
    return doc


class Collector:
    """Runs nvidia-smi on a timer and keeps the latest result (thread-safe)."""

    def __init__(self, nvidia_smi: str, poll_seconds: float, timeout_seconds: float = 10.0):
        self.nvidia_smi = nvidia_smi
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._samples: list[GpuSample] = []
        self._up = False
        self._updated_at: float | None = None
        self._error: str | None = "no sample yet"
        self._stop = threading.Event()

    def poll_once(self) -> None:
        cmd = [self.nvidia_smi, f"--query-gpu={','.join(QUERY_FIELDS)}", "--format=csv,noheader,nounits"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"nvidia-smi exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}")
            samples = parse_csv(proc.stdout)
            if not samples:
                raise RuntimeError("nvidia-smi reported no GPUs")
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            with self._lock:
                was_up = self._up
                self._up, self._samples, self._error = False, [], str(exc)
            if was_up or self._updated_at is None:
                log.warning("GPU query failed: %s", exc)
            return
        with self._lock:
            if not self._up:
                log.info("GPU query ok: %s", ", ".join(s.name for s in samples))
            self._samples, self._up, self._error = samples, True, None
            self._updated_at = time.time()

    def run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            self._stop.wait(max(0.0, self.poll_seconds - (time.monotonic() - started)))

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[list[GpuSample], bool, float | None, str | None]:
        with self._lock:
            return list(self._samples), self._up, self._updated_at, self._error


def make_handler(collector: Collector) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "forge-gpu-exporter"

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?", 1)[0]
            samples, up, updated_at, error = collector.snapshot()
            if path == "/metrics":
                self._send(200, "text/plain; version=0.0.4; charset=utf-8", render_metrics(samples, up))
            elif path == "/gpu":
                body = json.dumps(render_json(samples, up, updated_at, error))
                self._send(200, "application/json", body)
            elif path == "/healthz":
                self._send(200, "text/plain; charset=utf-8", "ok\n")
            else:
                self._send(404, "text/plain; charset=utf-8", "not found\n")

        def _send(self, status: int, content_type: str, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            # Prometheus scrapes every 15 s; access logs would only be noise.
            pass

    return Handler


def find_nvidia_smi() -> str:
    """On WSL2 the runtime injects nvidia-smi into /usr/bin or /usr/lib/wsl/lib."""
    configured = os.environ.get("FORGE_NVIDIA_SMI")
    if configured:
        return configured
    return shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FORGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.environ.get("FORGE_EXPORTER_PORT", "9400"))
    poll_seconds = float(os.environ.get("FORGE_POLL_SECONDS", "5"))

    collector = Collector(find_nvidia_smi(), poll_seconds)
    collector.poll_once()  # serve real data from the first scrape
    threading.Thread(target=collector.run, name="collector", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(collector))
    server.daemon_threads = True

    def shutdown(signum, _frame) -> None:
        log.info("signal %s, shutting down", signum)
        collector.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("listening on :%d, polling %s every %.0fs", port, collector.nvidia_smi, poll_seconds)
    server.serve_forever()


if __name__ == "__main__":
    main()
