#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$script_dir/../Dockerfile" ]]; then
  default_project="$(cd "$script_dir/.." && pwd)"
else
  default_project=/home/alexey/Projects/qwen-serving
fi
project_dir="${PROJECT_DIR:-$default_project}"
cd "$project_dir"
image="${IMAGE:-qwen-serving:dev}"
env_file="${ENV_FILE:-$project_dir/.env}"
volume="${MODEL_VOLUME:-qwen-models}"
container="qwen-docker-check-$(date +%s)-$$"
fresh_volume=0
case "${1:-}" in
  "") ;;
  --fresh) volume="$container-models"; fresh_volume=1 ;;
  *) echo "Usage: $0 [--fresh]" >&2; exit 2 ;;
esac
mkdir -p .test-results
exec > >(tee .test-results/docker-verification.log) 2>&1

cleanup() {
  result=$?
  trap - EXIT
  if docker inspect "$container" >/dev/null 2>&1; then
    docker logs "$container" > .test-results/container.log 2>&1 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
  fi
  if [[ "$fresh_volume" == 1 && "$result" == 0 ]]; then
    docker volume rm "$volume" >/dev/null 2>&1 || true
  elif [[ "$fresh_volume" == 1 ]]; then
    echo "Keeping model files for retry in volume: $volume"
    echo "Retry without --fresh using MODEL_VOLUME=$volume"
  fi
  if [[ "$result" != 0 ]]; then
    echo "FAIL: see .test-results/docker-verification.log; container diagnostics stay local."
  fi
  exit "$result"
}
trap cleanup EXIT

docker info >/dev/null
[[ -f "$env_file" ]] || { echo "Create .env first; see README."; exit 1; }
python3 -m unittest discover -s tests -v
bash -n scripts/run.sh scripts/entrypoint.sh scripts/init-volume.sh
docker build -t "$image" .

# Stop only the known project demo to free RAM for the test model.
if docker inspect qwen >/dev/null 2>&1; then
  demo_image="$(docker inspect --format "{{.Config.Image}}" qwen)"
  case "$demo_image" in
    qwen-serving:*|"$image")
      echo "Stopping demo container qwen before CPU checks."
      docker stop qwen >/dev/null
      ;;
    *) echo "Container qwen uses another image; refusing to stop it."; exit 1 ;;
  esac
fi

MODEL_VOLUME="$volume" IMAGE="$image" ./scripts/init-volume.sh
uid="$(docker run --rm --entrypoint id "$image" -u)"
[[ "$uid" == 10001 ]] || { echo "FAIL: expected UID 10001"; exit 1; }
if docker run --rm --entrypoint /app/scripts/entrypoint.sh "$image" > .test-results/no-token.log 2>&1; then
  echo "FAIL: startup without API token was accepted"; exit 1
fi
grep -q "VLLM_API_KEY is required" .test-results/no-token.log
echo "PASS: image rejects startup without an API token"

wait_ready() {
  startup_timeout="${STARTUP_TIMEOUT:-1200}"
  [[ "$startup_timeout" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid STARTUP_TIMEOUT"; return 1; }
  deadline=$((SECONDS + startup_timeout))
  next_progress=$SECONDS
  while (( SECONDS < deadline )); do
    state="$(docker inspect --format "{{.State.Status}}" "$container" 2>/dev/null || true)"
    [[ "$state" == running ]] || { echo "FAIL: container exited during startup"; return 1; }
    health="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    if [[ "$health" == healthy ]]; then
      return 0
    fi
    if (( SECONDS >= next_progress )); then
      echo "Waiting for model server; up to $((deadline - SECONDS))s remaining."
      next_progress=$((SECONDS + 30))
    fi
    sleep 3
  done
  echo "FAIL: startup timeout"
  return 1
}

echo "Starting test container; initial downloads may take several minutes."
docker run -d --name "$container" --env-file "$env_file" --mount "source=$volume,target=/models" -p 127.0.0.1::8000 --shm-size=1g --cap-drop=ALL --security-opt no-new-privileges=true -e VLLM_CPU_KVCACHE_SPACE=1 -e VLLM_CPU_OMP_THREADS_BIND=nobind -e OMP_NUM_THREADS=4 "$image"
wait_ready
address="$(docker port "$container" 8000/tcp)"
CONTAINER_NAME="$container" BASE_URL="http://$address" ENV_FILE="$env_file" python3 scripts/check_service.py
docker exec "$container" python /app/scripts/verify_dependencies.py /app/requirements.txt
docker logs "$container" > .test-results/container-first-start.log 2>&1
docker stop "$container" >/dev/null
docker rm "$container" >/dev/null

echo "Restarting with networking completely disabled."
docker run -d --name "$container" --network none --env-file "$env_file" --mount "source=$volume,target=/models" --shm-size=1g --cap-drop=ALL --security-opt no-new-privileges=true -e HF_HUB_OFFLINE=1 -e VLLM_CPU_KVCACHE_SPACE=1 -e VLLM_CPU_OMP_THREADS_BIND=nobind -e OMP_NUM_THREADS=4 "$image"
wait_ready
docker logs "$container" > .test-results/container-offline.log 2>&1
grep -q "Model verified locally; no network access required." .test-results/container-offline.log
echo "PASS: model server restarted from persistent storage with no network"
echo "PASS: Docker verification complete"
