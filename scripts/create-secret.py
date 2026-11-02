"""Create/update the dedicated demo Secret without putting tokens in argv/stdout."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
values = {}
path = Path(os.environ.get("ENV_FILE", root / ".env"))
if path.is_file():
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
for key in ("VLLM_API_KEY", "HF_TOKEN"):
    values[key] = os.environ.get(key, values.get(key, ""))
if not values["VLLM_API_KEY"]:
    sys.exit("Set a non-empty VLLM_API_KEY in .env first.")
secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {
    "name": "qwen-secrets", "namespace": "qwen-serving"}, "type": "Opaque",
    "stringData": {key: values[key] for key in ("VLLM_API_KEY", "HF_TOKEN")}}
# Server-side apply avoids the client-side last-applied annotation with secret text.
context = os.environ.get("KUBE_CONTEXT", "qwen-serving")
result = subprocess.run(["kubectl", "--context", context, "apply", "--server-side",
                         "--field-manager=qwen-secret", "-f", "-"],
                        input=json.dumps(secret), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if result.returncode:
    sys.exit("Secret update failed; check cluster connectivity and permissions.")
print("Secret qwen-secrets configured; credentials were not printed.")
