"""Check a running Docker or Kubernetes service without printing secrets or response text."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]


def api_key():
    if os.environ.get("VLLM_API_KEY"):
        return os.environ["VLLM_API_KEY"]
    path = Path(os.environ.get("ENV_FILE", ROOT / ".env"))
    for line in path.read_text().splitlines():
        if line.startswith("VLLM_API_KEY="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    raise RuntimeError("VLLM_API_KEY is missing")


def request(opener, url, token=None, payload=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=300) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    token = api_key()
    base = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    container = os.environ.get("CONTAINER_NAME", "qwen")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    since = datetime.now(timezone.utc).isoformat()

    kubernetes = os.environ.get("KUBERNETES_NAMESPACE")
    if kubernetes:
        command = ["kubectl", "--context", os.environ.get("KUBE_CONTEXT", "qwen-serving"),
                   "-n", kubernetes]
        user = subprocess.check_output(
            command + ["exec", "deployment/qwen", "-c", "vllm", "--", "id", "-u"],
            text=True).strip()
    else:
        user = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Config.User}}", container], text=True).strip()
    require(user in {"app", "10001", "10001:10001", "app:app"}, "Container must use app/10001")
    require(request(opener, base + "/health")[0] == 200, "Health check failed")
    wrong_token = "wrong-" + uuid.uuid4().hex
    require(request(opener, base + "/v1/models")[0] == 401, "Missing token must return 401")
    require(request(opener, base + "/v1/models", wrong_token)[0] == 401,
            "Wrong token must return 401")
    require(request(opener, base + "/v1/models", token)[0] == 200,
            "Correct token must allow access")
    print("PASS: health, non-root user, correct/missing/wrong API tokens", flush=True)

    marker = "PRIVATE_PROMPT_" + uuid.uuid4().hex
    started = time.monotonic()
    status, body = request(opener, base + "/v1/chat/completions", token, {
        "model": "qwen", "messages": [{"role": "user", "content": marker + " Say hello."}],
        "max_tokens": 8, "temperature": 0,
    })
    require(status == 200, "Inference failed")
    answer = json.loads(body)["choices"][0]["message"]["content"] or ""
    require(bool(answer.strip()), "Model returned no text")
    print(f"PASS: model answered in {time.monotonic() - started:.1f}s", flush=True)

    bad_marker = "PRIVATE_INVALID_" + uuid.uuid4().hex
    status, _ = request(opener, base + "/v1/chat/completions", token, {
        "model": "qwen", "messages": bad_marker,
    })
    require(status in {400, 422}, "Invalid request must be rejected")
    path_marker = "PRIVATE_PATH_" + uuid.uuid4().hex
    request(opener, base + "/" + path_marker, token)

    time.sleep(0.2)
    log_command = (command + ["logs", "deployment/qwen", "-c", "vllm", "--since-time", since]
                   if kubernetes else ["docker", "logs", "--since", since, container])
    logs = subprocess.check_output(log_command, stderr=subprocess.STDOUT, text=True)
    forbidden = [token, wrong_token, marker, bad_marker, path_marker]
    if len(answer.strip()) >= 8:
        forbidden.append(answer.strip())
    require(not any(value in logs for value in forbidden), "Private content found in logs")
    records = []
    for line in logs.splitlines():
        # vLLM workers may prefix a JSON record with their process name.
        start = line.find("{")
        if start < 0:
            continue
        try:
            records.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            continue
    require(any(record.get("event") == "request_finished"
                and record.get("route") == "/v1/chat/completions"
                and record.get("status") == 200 for record in records),
            "Successful inference must have an audit record")
    require(any(record.get("event") == "request_finished"
                and record.get("status") == 401 for record in records),
            "Authentication rejections must have audit records")
    require(any(record.get("event") == "request_finished"
                and record.get("status") in {400, 422} for record in records),
            "Invalid requests must have audit records")
    print("PASS: successful/rejected requests audited; private contents absent from logs")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exception text from HTTP or Docker may contain sensitive material.
        message = str(error) if type(error) is RuntimeError else "Inspect local service configuration."
        print(f"FAIL ({type(error).__name__}): {message}", file=sys.stderr)
        sys.exit(1)
