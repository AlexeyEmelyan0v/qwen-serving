#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-$project_dir/.env}"
if [[ ! -f "$env_file" ]]; then
  echo 'Create .env from .env.example and set VLLM_API_KEY first.' >&2
  exit 1
fi

exec docker run --rm --name "${CONTAINER_NAME:-qwen}" \
  --env-file "$env_file" \
  --mount "source=${MODEL_VOLUME:-qwen-models},target=/models" \
  -p "127.0.0.1:${HOST_PORT:-8000}:8000" \
  --shm-size=1g \
  --cap-drop=ALL \
  --security-opt no-new-privileges=true \
  -e VLLM_CPU_KVCACHE_SPACE="${VLLM_CPU_KVCACHE_SPACE:-1}" \
  -e VLLM_CPU_OMP_THREADS_BIND=nobind \
  -e OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" \
  "${IMAGE:-qwen-serving:dev}" "$@"
