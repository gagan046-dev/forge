#!/bin/sh
# Runs at every node start (k3d executes /bin/k3d-entrypoint-*.sh in order).
#
# Why: on Windows, the GPU reaches Linux through WSL2. A WSL2 GPU is not
# /dev/nvidia0 but /dev/dxg, and the NVIDIA libraries (libcuda, libnvidia-ml,
# libdxcore, nvidia-smi) live in /usr/lib/wsl/lib.
# Docker Desktop mounts those files into the node container, but under
# /usr/lib/x86_64-linux-gnu. The NVIDIA Container Toolkit inside the node only
# looks in /usr/lib/wsl/lib when it prepares a pod, so without these links
# pods get the device but not libdxcore.so and NVML fails ("Not Supported").
set -o errexit

[ -e /dev/dxg ] || { echo "[wsl-gpu-libs] no /dev/dxg, not a WSL2 GPU node, skipping"; exit 0; }

mkdir -p /usr/lib/wsl/lib
# Every GPU file Docker Desktop mounted shows up as its own mount point.
for f in $(awk '$2 ~ /^\/usr\/(lib\/x86_64-linux-gnu|bin)\// && ($3 == "9p" || $3 == "overlay") {print $2}' /proc/mounts); do
  ln -sf "$f" "/usr/lib/wsl/lib/$(basename "$f")"
  echo "[wsl-gpu-libs] linked $f"
done
