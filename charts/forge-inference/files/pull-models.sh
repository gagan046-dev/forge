#!/bin/sh
# Pulls every model in $FORGE_MODELS (space-separated Ollama tags) into $OLLAMA_MODELS.
# Idempotent: a model that is already on the volume is skipped without touching the
# network, so a restart (or a laptop that is offline) does not block Ollama.
# Used by the ollama-pull Job and by the Ollama pod's init container.
set -eu

# "ollama pull" is a client of the Ollama server, so start a private one on a
# loopback port that cannot clash with anything else.
export OLLAMA_HOST=127.0.0.1:11435
ollama serve >/tmp/ollama-pull-serve.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT

tries=0
until ollama list >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    echo "[pull] temporary ollama server did not start:" >&2
    cat /tmp/ollama-pull-serve.log >&2
    exit 1
  fi
  sleep 1
done

for model in $FORGE_MODELS; do
  if ollama show "$model" >/dev/null 2>&1; then
    echo "[pull] $model is already present"
  else
    echo "[pull] pulling $model"
    ollama pull "$model"
  fi
done
echo "[pull] models ready:"
ollama list
