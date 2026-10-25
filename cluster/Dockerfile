# The k3s node image for the Forge cluster: a k3s node that can hand the GPU to pods.
# Adapted from gpu-hello/k3d (verified on this laptop's RTX 4070 under WSL2).
#
# The stock rancher/k3s image is busybox-based with no glibc, so the NVIDIA
# Container Toolkit cannot run in it. We start from Ubuntu instead, install the
# toolkit, and copy only the k3s binaries on top.
#
# Build:  docker build -t forge/k3s-gpu:v1.35.9-k3s1 cluster/

ARG K3S_TAG=v1.35.9-k3s1
FROM rancher/k3s:${K3S_TAG} AS k3s

# Dated Ubuntu 24.04 tag, so a rebuild gives the same base.
FROM ubuntu:noble-20260917
ARG NVIDIA_TOOLKIT_VERSION=1.20.1-1

# 1. NVIDIA Container Toolkit from NVIDIA's apt repository (pinned version).
#    It provides nvidia-container-runtime, the wrapper around runc that
#    injects the GPU device and driver libraries into a container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit.gpg \
 && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit.gpg] https://#' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      nvidia-container-toolkit=${NVIDIA_TOOLKIT_VERSION} \
      nvidia-container-toolkit-base=${NVIDIA_TOOLKIT_VERSION} \
      libnvidia-container-tools=${NVIDIA_TOOLKIT_VERSION} \
      libnvidia-container1=${NVIDIA_TOOLKIT_VERSION} \
      iptables kmod mount \
 && apt-get purge -y gnupg curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# 2. k3s itself: one static binary plus its helpers (CNI plugins, runc, iptables
#    wrappers in /bin/aux). Ubuntu already has coreutils, so we skip busybox's tools,
#    which would otherwise overwrite Ubuntu's.
COPY --from=k3s /bin/k3s /bin/cni /bin/runc /bin/containerd-shim-runc-v2 \
                /bin/conntrack /bin/ipset /bin/ethtool /bin/ip /bin/pigz \
                /bin/fuse-overlayfs /bin/slirp4netns /bin/check-config /bin/busybox /bin/
COPY --from=k3s /bin/aux /bin/aux
COPY --from=k3s /var/lib/rancher/k3s/data /var/lib/rancher/k3s/data
RUN for t in kubectl crictl ctr containerd k3s-server k3s-agent; do ln -sf k3s /bin/$t; done \
 && ln -sf pigz /bin/unpigz \
 # Fix 1: k3d injects entrypoint scripts that use "trap ... SIGTERM". Ubuntu's
 # default /bin/sh (dash) rejects that syntax and the node crash-loops, so use bash.
 && ln -sf bash /bin/sh

# 3. Start-up hooks that k3d runs before k3s (each script explains its "why"):
#    - Fix 2: add a fallback DNS server so image pulls don't fail randomly
#    - Fix 3: expose Docker Desktop's WSL2 GPU libraries where the toolkit looks for them
COPY --chmod=0755 k3d-entrypoint-zz-wsl-gpu-libs.sh k3d-entrypoint-zz-dns-fallback.sh /bin/

# k3s looks for /usr/bin/nvidia-container-runtime at start-up and, if found,
# registers an extra containerd runtime and a RuntimeClass called "nvidia".
# Pods opt in to it with runtimeClassName: nvidia.

VOLUME /var/lib/kubelet /var/lib/rancher/k3s /var/lib/cni /var/log
ENV PATH="/var/lib/rancher/k3s/data/cni:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/bin/aux" \
    CRI_CONFIG_FILE=/var/lib/rancher/k3s/agent/etc/crictl.yaml
ENTRYPOINT ["/bin/k3s"]
CMD ["agent"]
