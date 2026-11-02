#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$project_dir/.tools"
# Local tools only; no system packages/configuration are modified.
python3 - "$project_dir/.tools" <<'PYTHON'
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
root = Path(sys.argv[1])
version = "v1.35.0"
urls = {
    "kubectl": f"https://dl.k8s.io/release/{version}/bin/linux/amd64/kubectl",
    "minikube": "https://github.com/kubernetes/minikube/releases/download/v1.38.1/minikube-linux-amd64",
}
for name, url in urls.items():
    data = urllib.request.urlopen(url, timeout=180).read()
    expected = urllib.request.urlopen(url + ".sha256", timeout=60).read().decode().split()[0]
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("Tool checksum mismatch: " + name)
    (root / name).write_bytes(data)
    (root / name).chmod(0o755)
    print("Installed verified local tool:", name)
PYTHON
