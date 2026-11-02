"""Verify a pinned model locally; download only absent or damaged files."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config/model-manifest.json"


def valid_file(path, entry):
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest() == entry["sha256"]


def prepare_model(model_dir, manifest):
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    with (model_dir / ".download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        invalid = [entry for entry in manifest["files"]
                   if not valid_file(model_dir / entry["path"], entry)]
        if not invalid:
            print("Model verified locally; no network access required.", flush=True)
            return
        if os.environ.get("HF_HUB_OFFLINE", "0").upper() in {"1", "ON", "YES", "TRUE"}:
            raise RuntimeError("Model files are missing or damaged in offline mode")

        # A valid local cache never imports or calls the HF client.
        from huggingface_hub import hf_hub_download

        for entry in invalid:
            print(f"Downloading model file: {entry['path']}", flush=True)
            hf_hub_download(
                repo_id=manifest["repo_id"],
                revision=manifest["revision"],
                filename=entry["path"],
                local_dir=str(model_dir),
                token=os.environ.get("HF_TOKEN") or None,
                force_download=True,
            )
            if not valid_file(model_dir / entry["path"], entry):
                raise RuntimeError("Downloaded model file failed its checksum")
        print("Pinned model download and checksum verification complete.", flush=True)


def main():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    prepare_model(os.environ.get("MODEL_DIR", "/models/qwen"), manifest)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Hub exception messages can contain URLs; never print credentials.
        print(f"Model preparation failed ({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)
