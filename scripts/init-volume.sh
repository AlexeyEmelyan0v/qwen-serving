#!/usr/bin/env bash
set -euo pipefail
volume="${MODEL_VOLUME:-qwen-models}"
image="${IMAGE:-qwen-serving:dev}"
docker volume create "$volume" >/dev/null
# One-time volume initialization only; the model server stays non-root.
docker run --rm --user 0:0 \
  --mount "source=$volume,target=/models" \
  --entrypoint chown "$image" 10001:10001 /models
echo "Model volume initialized: $volume"
