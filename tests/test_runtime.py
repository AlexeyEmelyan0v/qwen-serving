import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import logging.config
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.privacy import AuditMiddleware, PrivacyFormatter


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


downloader = load_script("download_model")
dependencies = load_script("verify_dependencies")
service_check = load_script("check_service")


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.data = b"valid-model-data"
        self.entry = {"path": "model.safetensors", "size": len(self.data),
                      "sha256": hashlib.sha256(self.data).hexdigest()}
        self.manifest = {"repo_id": "test/model", "revision": "a" * 40,
                         "files": [self.entry]}

    def test_valid_cache_makes_no_hub_calls_even_offline(self):
        (self.directory / self.entry["path"]).write_bytes(self.data)
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), \
                patch.dict(sys.modules, {"huggingface_hub": None}):
            downloader.prepare_model(self.directory, self.manifest)

    def test_same_size_corruption_is_redownloaded_at_pinned_revision(self):
        (self.directory / self.entry["path"]).write_bytes(b"x" * len(self.data))
        calls = []

        def download(**kwargs):
            calls.append(kwargs)
            (self.directory / kwargs["filename"]).write_bytes(self.data)

        hub = types.SimpleNamespace(hf_hub_download=download)
        with patch.dict(sys.modules, {"huggingface_hub": hub}), \
                patch.dict(os.environ, {"HF_HUB_OFFLINE": "0"}):
            downloader.prepare_model(self.directory, self.manifest)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["revision"], self.manifest["revision"])
        self.assertTrue(calls[0]["force_download"])

    def test_offline_missing_file_fails_before_importing_hub(self):
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), \
                patch.dict(sys.modules, {"huggingface_hub": None}):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                downloader.prepare_model(self.directory, self.manifest)

    def test_interrupted_download_is_repaired_on_retry(self):
        target = self.directory / self.entry["path"]

        def interrupted(**kwargs):
            target.write_bytes(b"partial")
            raise ConnectionError("interrupted")

        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "0"}):
            with patch.dict(sys.modules, {"huggingface_hub": types.SimpleNamespace(
                    hf_hub_download=interrupted)}):
                with self.assertRaises(ConnectionError):
                    downloader.prepare_model(self.directory, self.manifest)
            with patch.dict(sys.modules, {"huggingface_hub": types.SimpleNamespace(
                    hf_hub_download=lambda **kwargs: target.write_bytes(self.data))}):
                downloader.prepare_model(self.directory, self.manifest)
        self.assertTrue(downloader.valid_file(target, self.entry))

    def test_bad_download_is_rejected(self):
        hub = types.SimpleNamespace(hf_hub_download=lambda **kwargs: None)
        with patch.dict(sys.modules, {"huggingface_hub": hub}), \
                patch.dict(os.environ, {"HF_HUB_OFFLINE": "0"}):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                downloader.prepare_model(self.directory, self.manifest)


class PrivacyTests(unittest.TestCase):
    def test_arbitrary_messages_args_and_tracebacks_are_not_formatted(self):
        try:
            raise ValueError("PRIVATE_EXCEPTION")
        except ValueError:
            record = logging.LogRecord("vllm.engine", logging.ERROR, __file__, 1,
                                       "PRIVATE_PROMPT %s", ("PRIVATE_RESPONSE",),
                                       sys.exc_info())
        output = PrivacyFormatter().format(record)
        self.assertNotIn("PRIVATE_", output)
        self.assertEqual(json.loads(output)["event"], "runtime_log")

    def capture_request(self, status=200, fail=False, unknown_path=False):
        stream = io.StringIO()
        config = json.loads((ROOT / "config/logging.json").read_text())
        config["handlers"]["private"]["stream"] = stream
        logging.config.dictConfig(config)

        async def app(scope, receive, send):
            await receive()
            if fail:
                raise RuntimeError("PRIVATE_EXCEPTION")
            await send({"type": "http.response.start", "status": status})
            await send({"type": "http.response.body", "body": b"PRIVATE_RESPONSE"})

        async def receive():
            return {"type": "http.request", "body": b"PRIVATE_PROMPT"}

        async def send(message):
            pass

        scope = {"type": "http", "method": "POST",
                 "path": "/PRIVATE_PATH" if unknown_path else "/v1/chat/completions",
                 "query_string": b"PRIVATE_QUERY",
                 "headers": [(b"authorization", b"Bearer PRIVATE_TOKEN")]}
        try:
            asyncio.run(AuditMiddleware(app)(scope, receive, send))
        except RuntimeError:
            if not fail:
                raise
        output = stream.getvalue()
        self.assertNotIn("PRIVATE_", output)
        return [json.loads(line) for line in output.splitlines()]

    def test_streamed_response_is_not_logged_but_request_is(self):
        records = self.capture_request()
        self.assertEqual([r["event"] for r in records],
                         ["request_started", "request_finished"])
        self.assertEqual(records[-1]["status"], 200)
        self.assertEqual(records[0]["request_id"], records[1]["request_id"])

    def test_auth_rejection_is_audited(self):
        self.assertEqual(self.capture_request(status=401)[-1]["status"], 401)

    def test_exception_payload_and_unknown_path_are_not_logged(self):
        record = self.capture_request(fail=True, unknown_path=True)[-1]
        self.assertEqual(record["event"], "request_failed")
        self.assertEqual(record["route"], "other")


class DependencyTests(unittest.TestCase):
    def test_full_lock_is_pinned(self):
        locked = dependencies.locked_versions(ROOT / "requirements.txt")
        self.assertGreater(len(locked), 100)
        self.assertEqual(locked["vllm"], "0.27.0+cpu")

    def test_unpinned_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text("vllm>=0.27.0\n")
            with self.assertRaises(ValueError):
                dependencies.locked_versions(path)

    def test_lock_rejects_changed_or_extra_installed_packages(self):
        locked = dependencies.locked_versions(ROOT / "requirements.txt")
        distributions = [types.SimpleNamespace(metadata={"Name": name}, version=version)
                         for name, version in locked.items()]
        with patch.object(dependencies.metadata, "distributions", return_value=distributions), \
                patch.object(sys, "argv", ["verify", str(ROOT / "requirements.txt")]):
            with contextlib.redirect_stdout(io.StringIO()):
                dependencies.main()
            distributions.append(types.SimpleNamespace(metadata={"Name": "unexpected"}, version="1"))
            with self.assertRaises(SystemExit):
                dependencies.main()


class StartupTests(unittest.TestCase):
    def test_server_refuses_empty_api_key_before_loading_model(self):
        env = {**os.environ, "VLLM_API_KEY": ""}
        result = subprocess.run(["bash", str(ROOT / "scripts/entrypoint.sh")],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VLLM_API_KEY is required", result.stderr)

    def test_private_logging_cannot_be_overridden(self):
        for flag in ["--enable-log-requests", "--enable-log-outputs",
                     "--api-key=another", "--config=unsafe.yaml"]:
            with self.subTest(flag=flag):
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts/entrypoint.sh"), flag],
                    env={**os.environ, "VLLM_API_KEY": "test-only"},
                    capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Conflicting", result.stderr)


class ServiceCheckTests(unittest.TestCase):
    def run_checker(self, leak=False, open_api=False):
        token = "test-api-token-never-log"

        def request(opener, url, supplied_token=None, payload=None):
            if url.endswith("/health"):
                return 200, b""
            if supplied_token != token:
                return (200 if open_api else 401), b""
            if payload and isinstance(payload.get("messages"), str):
                return 422, b""
            if payload:
                return 200, json.dumps({"choices": [{"message": {
                    "content": "A short generated answer."}}]}).encode()
            return 200, b""

        records = [
            {"event": "request_finished", "route": "/v1/chat/completions", "status": 200},
            {"event": "request_finished", "route": "/v1/models", "status": 401},
            {"event": "request_finished", "route": "/v1/chat/completions", "status": 422},
        ]
        logs = "\n".join(json.dumps(record) for record in records)
        if leak:
            logs += token
        with patch.object(service_check, "api_key", return_value=token), \
                patch.object(service_check, "request", side_effect=request), \
                patch.object(service_check.subprocess, "check_output", side_effect=["app\n", logs]), \
                patch.object(service_check.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            service_check.main()

    def test_checker_accepts_authenticated_private_service(self):
        self.run_checker()

    def test_checker_detects_missing_authentication(self):
        with self.assertRaisesRegex(RuntimeError, "401"):
            self.run_checker(open_api=True)

    def test_checker_detects_private_log_leak(self):
        with self.assertRaisesRegex(RuntimeError, "Private content"):
            self.run_checker(leak=True)


if __name__ == "__main__":
    unittest.main()
