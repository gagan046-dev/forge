# Forge gateway image: FastAPI on uvicorn, non-root (uid 10001), works with readOnlyRootFilesystem
# (mount an emptyDir at /tmp for the RAG module's git clones).
#
# Build:  docker build -t forge-gateway gateway/
# Run:    uvicorn (default CMD); migrations: `python -m app.migrate` (Helm hook Job).

ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
# --require-hashes: every wheel must match the hash pinned in uv.lock.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt

FROM ${PYTHON_IMAGE}
# git: the RAG indexer clones repositories; ca-certificates: HTTPS for those clones.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin forge

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# Compile once at build time, since the root filesystem is read-only at runtime.
RUN python -m compileall -q /app/app /app/alembic

USER 10001:10001
EXPOSE 8000
# --proxy-headers: client IPs (login rate limit) come from Traefik's X-Forwarded-For. Traefik
# replaces untrusted incoming X-Forwarded-* headers, and NetworkPolicy only lets the ingress in.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
