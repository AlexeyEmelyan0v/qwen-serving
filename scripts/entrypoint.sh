#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${VLLM_API_KEY:-}" ]]; then
  echo 'VLLM_API_KEY is required; pass it through an environment file or a Secret.' >&2
  exit 1
fi

# Enforce privacy even when CMD is overridden.
for arg in "$@"; do
  case "$arg" in
    --api-key|--api-key=*|--enable-log-requests*|--enable-log-outputs*|--enable-log-deltas*|--log-error-stack*|--middleware*|--log-config-file*|--uvicorn-log-level*|--no-disable-uvicorn-access-log*|--config|--config=*)
      echo 'Conflicting authentication or logging argument; see README.' >&2
      exit 1
      ;;
  esac
done

export VLLM_CONFIGURE_LOGGING=1
export VLLM_LOGGING_LEVEL=INFO
export VLLM_LOGGING_CONFIG_PATH=/app/config/logging.json
export VLLM_DEBUG_LOG_API_SERVER_RESPONSE=0
export VLLM_TRACE_FUNCTION=0

echo 'Checking local model files...'
python -u /app/scripts/download_model.py

unset HF_TOKEN HUGGING_FACE_HUB_TOKEN
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

echo 'Starting vLLM; health endpoint: /health'
exec vllm serve "${MODEL_DIR:-/models/qwen}" "$@" \
  --no-enable-log-requests \
  --no-enable-log-outputs \
  --no-enable-log-deltas \
  --no-log-error-stack \
  --disable-uvicorn-access-log \
  --log-config-file /app/config/logging.json \
  --middleware runtime.privacy.AuditMiddleware
