# forge-gpu-exporter: nvidia-smi -> Prometheus metrics (:9400/metrics) and JSON (:9400/gpu).
#
# A glibc base (Debian slim) is required: on WSL2 the NVIDIA runtime injects the
# host's nvidia-smi and driver libraries into the container, and they need glibc.
# Build:  docker build -t forge-gpu-exporter:dev gpu-exporter/
FROM python:3.12.14-slim-bookworm

# Ask the NVIDIA runtime to inject the driver utilities (nvidia-smi, NVML) although the
# pod requests no nvidia.com/gpu: "utility" is all a monitor needs, and the only GPU
# stays allocatable for Ollama.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=utility \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY gpu_exporter.py ./

# Fixed non-root uid/gid so Pod Security "restricted" (runAsNonRoot) can verify it.
RUN groupadd --system --gid 10001 exporter \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin exporter
USER 10001:10001

EXPOSE 9400
ENTRYPOINT ["python", "/app/gpu_exporter.py"]
