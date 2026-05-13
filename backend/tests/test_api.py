from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.api import create_app
from repo_maintainer.config import PlatformConfig


class FakeCiApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/gitlab/projects/123":
            self._json(
                {
                    "id": 123,
                    "path_with_namespace": "octo/example",
                    "http_url_to_repo": "https://gitlab.example/octo/example.git",
                    "web_url": "https://gitlab.example/octo/example",
                    "default_branch": "main",
                }
            )
            return
        if self.path.startswith("/gitlab/projects/123/pipelines?"):
            self._json(
                [
                    {
                        "id": 888,
                        "name": "CI",
                        "status": "failed",
                        "ref": "main",
                        "sha": "abc123",
                        "web_url": "https://gitlab.example/pipelines/888",
                        "created_at": "2026-04-20T08:00:00Z",
                        "updated_at": "2026-04-20T08:02:00Z",
                    }
                ]
            )
            return
        if self.path == "/gitlab/projects/123/pipelines/888/jobs":
            self._json([{"id": 998, "name": "pytest", "status": "failed"}])
            return
        if self.path == "/gitlab/projects/123/jobs/998/trace":
            self._text("Traceback: gitlab failure\nFAILED tests/test_service.py")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, payload: str) -> None:
        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def fake_ci_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCiApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/gitlab"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def temporary_env(**entries: str):
    original = {key: os.environ.get(key) for key in entries}
    os.environ.update(entries)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ApiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = ROOT / ".test_runs" / f"api-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        self.config = PlatformConfig.default(test_root)
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def test_dashboard_and_provider_endpoints(self) -> None:
        overview = self.client.get("/api/dashboard/overview")
        providers = self.client.get("/api/providers")
        repos = self.client.get("/api/repos")
        observability = self.client.get("/api/observability")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(providers.status_code, 200)
        self.assertEqual(repos.status_code, 200)
        self.assertEqual(observability.status_code, 200)
        self.assertIn("hero_metrics", overview.json())
        self.assertIn("catalog", providers.json())
        self.assertGreaterEqual(len(repos.json()), 1)
        self.assertIn("learning_status", observability.json())

    def test_ci_provider_and_sync_endpoints(self) -> None:
        with fake_ci_server() as gitlab_url:
            with temporary_env(GITLAB_TOKEN="test-token", GITLAB_API_URL=gitlab_url, GITLAB_PROJECT_ID="123"):
                config = PlatformConfig.default(ROOT / ".test_runs" / f"api-ci-{uuid4().hex}")
                config = replace(config, gitlab=replace(config.gitlab, token="test-token", enabled=True))
                app = create_app(config)
                client = TestClient(app)
                providers = client.get("/api/ci/providers")
                sync = client.post("/api/ci/sync", json={"provider": "gitlab_ci", "project_id": "123", "limit": 1})
                incidents = client.get("/api/incidents", params={"provider": "gitlab_ci"})

        self.assertEqual(providers.status_code, 200)
        self.assertEqual(sync.status_code, 200)
        self.assertEqual(incidents.status_code, 200)
        self.assertEqual(sync.json()["provider"], "gitlab_ci")
        self.assertEqual(len(sync.json()["runs"]), 1)
        self.assertTrue(all(item["provider"] == "gitlab_ci" for item in incidents.json()))

    def test_repo_create_and_contexts(self) -> None:
        created = self.client.post(
            "/api/repos",
            json={
                "name": "Frontend Demo Repo",
                "url": "https://example.com/repo",
                "owner": "QA",
                "status": "normal",
                "ci_status": "normal",
                "agent_name": "maintainer-orchestrator",
                "description": "Created from API smoke test.",
                "local_path": str(self.config.root_dir),
            },
        )
        contexts = self.client.get("/api/contexts")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(contexts.status_code, 200)
        self.assertEqual(created.json()["name"], "Frontend Demo Repo")
        self.assertIn("working_contexts", contexts.json())
