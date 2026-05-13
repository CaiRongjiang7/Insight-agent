from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.config import PlatformConfig
from repo_maintainer.frontend_service import FrontendDataService
from repo_maintainer.models import ApprovalScope
from repo_maintainer.platform import RepoAutonomyPlatform


def build_fix_payload() -> dict[str, object]:
    pytest_command = f'"{sys.executable}" -m pytest -q' if importlib.util.find_spec("pytest") else f'"{sys.executable}" -m unittest discover -s tests -v'
    return {
        "analysis": "Restore the service response contract expected by the test.",
        "summary": "Repair the service layer so the response exposes `name` instead of `userName`.",
        "commit_message": "fix: restore service response contract",
        "validation_commands": [pytest_command],
        "edits": [
            {
                "path": "app/service.py",
                "operation": "rewrite",
                "reason": "The test expects `name` in the response payload.",
                "content": (
                    "from app.repository import UserRepository\n\n\n"
                    "def get_user_summary(user_id: int) -> dict[str, object]:\n"
                    "    repository = UserRepository()\n"
                    "    record = repository.get_user(user_id)\n"
                    "    return {\"name\": record[\"name\"], \"id\": record[\"id\"]}\n"
                ),
            }
        ],
    }


def build_bad_payload() -> dict[str, object]:
    pytest_command = f'"{sys.executable}" -m pytest -q' if importlib.util.find_spec("pytest") else f'"{sys.executable}" -m unittest discover -s tests -v'
    return {
        "analysis": "Return the same broken payload shape.",
        "summary": "Keep the broken contract.",
        "commit_message": "fix: attempt broken patch",
        "validation_commands": [pytest_command],
        "edits": [
            {
                "path": "app/service.py",
                "operation": "rewrite",
                "reason": "Bad patch for rollback test.",
                "content": (
                    "from app.repository import UserRepository\n\n\n"
                    "def get_user_summary(user_id: int) -> dict[str, object]:\n"
                    "    repository = UserRepository()\n"
                    "    record = repository.get_user(user_id)\n"
                    "    return {\"userName\": record[\"name\"], \"id\": record[\"id\"]}\n"
                ),
            }
        ],
    }


class FakeResponsesHandler(BaseHTTPRequestHandler):
    payload_queue: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        if self.path == "/v1/embeddings":
            request = json.loads(raw_body.decode("utf-8"))
            inputs = request.get("input", [])
            if not isinstance(inputs, list):
                inputs = [inputs]
            response = {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": [round(0.01 * (index + 1), 4), round(0.02 * (index + 1), 4), round(0.03 * (index + 1), 4)],
                    }
                    for index, _ in enumerate(inputs)
                ],
                "model": "text-embedding-3-small",
            }
        else:
            payload = self.payload_queue.pop(0)
            if self.path.endswith("/chat/completions"):
                response = {
                    "id": "chatcmpl_test",
                    "model": "gpt-5.2",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(payload, ensure_ascii=False),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 111, "completion_tokens": 222, "total_tokens": 333},
                }
            else:
                response = {
                    "id": "resp_test",
                    "model": "gpt-5.2",
                    "output_text": json.dumps(payload, ensure_ascii=False),
                    "usage": {"input_tokens": 111, "output_tokens": 222, "total_tokens": 333},
                }
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class FakeJenkinsRepairHandler(BaseHTTPRequestHandler):
    repo_url: str = ""
    revision: str = ""
    branch: str = "main"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/job/sample/api/json"):
            self._json({"builds": [{"number": 45, "url": "http://jenkins/job/sample/45/", "result": "FAILURE", "timestamp": 1}]})
            return
        if self.path == "/job/sample/config.xml":
            self._text(
                f"""
<flow-definition>
  <definition>
    <script>
pipeline {{
  agent any
  stages {{
    stage('Test') {{
      steps {{
        sh 'python -m pytest -q'
      }}
    }}
  }}
}}
    </script>
  </definition>
  <scm>
    <userRemoteConfigs>
      <hudson.plugins.git.UserRemoteConfig>
        <url>{self.repo_url}</url>
      </hudson.plugins.git.UserRemoteConfig>
    </userRemoteConfigs>
  </scm>
</flow-definition>
                """.strip()
            )
            return
        if self.path.startswith("/job/sample/45/api/json"):
            self._json(
                {
                    "actions": [
                        {
                            "remoteUrls": [self.repo_url],
                            "lastBuiltRevision": {
                                "SHA1": self.revision,
                                "branch": [{"name": f"origin/{self.branch}", "SHA1": self.revision}],
                            },
                        }
                    ],
                    "changeSet": {"items": [{"commitId": self.revision}]},
                }
            )
            return
        if self.path == "/job/sample/45/consoleText":
            self._text("Traceback: jenkins failure\nFAILED tests/test_service.py")
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
def fake_openai_server(payloads: list[dict[str, object]]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponsesHandler)
    FakeResponsesHandler.payload_queue = list(payloads)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def fake_jenkins_server(repo_url: str, revision: str, branch: str = "main"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJenkinsRepairHandler)
    FakeJenkinsRepairHandler.repo_url = repo_url
    FakeJenkinsRepairHandler.revision = revision
    FakeJenkinsRepairHandler.branch = branch
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
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


def create_git_repo_from_sample(destination: Path) -> str:
    shutil.copytree(ROOT / "examples" / "sample_repo", destination)
    subprocess.run(["git", "init"], cwd=destination, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-B", "main"], cwd=destination, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "tests@example.local"], cwd=destination, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Repair Tests"], cwd=destination, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=destination, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial failing state"], cwd=destination, check=True, capture_output=True, text=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=destination, check=True, capture_output=True, text=True).stdout.strip()


class FakeRemoteDelivery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def deliver(self, **kwargs) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "status": "created",
            "provider": "github",
            "branch": "agent/fix-test",
            "url": "https://github.com/octo/example/pull/1",
        }


class RepairPipelineTests(unittest.TestCase):
    def test_repair_pipeline_can_clone_repo_from_jenkins_metadata(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        remote_repo = test_root / "remote-sample-repo"
        revision = create_git_repo_from_sample(remote_repo)
        with fake_openai_server([build_fix_payload()]) as base_url:
            with fake_jenkins_server(str(remote_repo), revision) as jenkins_url:
                with temporary_env(
                    OPENAI_API_KEY="test-key",
                    OPENAI_BASE_URL=base_url,
                    JENKINS_BASE_URL=jenkins_url,
                    JENKINS_JOB_NAME="sample",
                ):
                    config = PlatformConfig.default(test_root)
                    config = replace(
                        config,
                        openai=replace(config.openai, api_key="test-key", base_url=base_url, compatibility_mode="openai_responses"),
                        jenkins=replace(config.jenkins, base_url=jenkins_url, default_job="sample", enabled=True),
                        agent_reasoning=replace(config.agent_reasoning, enabled=False),
                    )
                    platform = RepoAutonomyPlatform(config)
                    platform.approve(
                        ApprovalScope.REMOTE_EXECUTION,
                        "*",
                        "tester",
                        "allow repair for auto-materialized workspaces",
                    )
                    sync = platform.sync_ci_failures("jenkins", limit=1)
                    outcome = platform.repair_incident(sync.incidents[0], max_attempts=1)

        self.assertEqual(sync.incidents[0].metadata["repo_url"], str(remote_repo))
        self.assertEqual(sync.incidents[0].metadata["head_sha"], revision)
        self.assertTrue(outcome.success)
        self.assertTrue(outcome.final_validation.success)
        self.assertIsNotNone(outcome.commit_hash)
        self.assertTrue(any(path.is_dir() for path in platform.config.paths.checkouts_dir.iterdir()))
        self.assertEqual(outcome.trace.context_digest["repo_workspace"]["source"], "ci_metadata")
        self.assertEqual(outcome.trace.context_digest["repo_workspace"]["revision"], revision)
        repaired_text = (Path(outcome.sandbox_path) / "app" / "service.py").read_text(encoding="utf-8")
        self.assertIn('"name"', repaired_text)

    def test_retry_refreshes_jenkins_metadata_before_remote_delivery(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        remote_repo = test_root / "remote-sample-repo"
        revision = create_git_repo_from_sample(remote_repo)
        with fake_openai_server([build_fix_payload()]) as base_url:
            with fake_jenkins_server(str(remote_repo), revision) as jenkins_url:
                with temporary_env(
                    OPENAI_API_KEY="test-key",
                    OPENAI_BASE_URL=base_url,
                    JENKINS_BASE_URL=jenkins_url,
                    JENKINS_JOB_NAME="sample",
                ):
                    config = PlatformConfig.default(test_root)
                    config = replace(
                        config,
                        openai=replace(config.openai, api_key="test-key", base_url=base_url, compatibility_mode="openai_responses"),
                        jenkins=replace(config.jenkins, base_url=jenkins_url, default_job="sample", enabled=True),
                        agent_reasoning=replace(config.agent_reasoning, enabled=False),
                    )
                    platform = RepoAutonomyPlatform(config)
                    delivery = FakeRemoteDelivery()
                    platform.remote_delivery = delivery
                    service = FrontendDataService(platform)
                    stale_incident = {
                        "id": "jenkins-build-45",
                        "title": "Jenkins failure: sample #45",
                        "description": "Old incident without SCM metadata.",
                        "logs": "old logs",
                        "changed_files": [],
                        "suspected_modules": [],
                        "metadata": {
                            "provider": "jenkins",
                            "job_name": "sample",
                            "build_number": "45",
                        },
                    }
                    incident_path = config.paths.incidents_dir / "jenkins-build-45.json"
                    incident_path.write_text(json.dumps(stale_incident), encoding="utf-8")

                    result = service.retry_incident("jenkins-build-45", max_attempts=1)
                    delivery_result = service.deliver_repair_trace(str(result["trace_id"]))

        persisted = json.loads(incident_path.read_text(encoding="utf-8"))
        prepared = delivery.calls[0]["prepared"]
        self.assertTrue(result["success"])
        self.assertEqual(result["delivery"]["status"], "skipped")
        self.assertEqual(result["delivery"]["reason"], "remote_delivery_not_confirmed")
        self.assertTrue(result["remote_delivery_pending"])
        self.assertTrue(delivery_result["success"])
        self.assertEqual(delivery_result["delivery"]["status"], "created")
        self.assertEqual(persisted["metadata"]["repo_url"], str(remote_repo))
        self.assertEqual(persisted["metadata"]["head_sha"], revision)
        self.assertEqual(prepared.incident.metadata["repo_url"], str(remote_repo))
        self.assertEqual(prepared.incident.metadata["head_sha"], revision)

    def test_repair_pipeline_generates_commit_in_sandbox(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_openai_server([build_fix_payload()]) as base_url:
            with temporary_env(OPENAI_API_KEY="test-key", OPENAI_BASE_URL=base_url):
                config = PlatformConfig.default(test_root)
                config = replace(
                    config,
                    openai=replace(config.openai, api_key="test-key", base_url=base_url, compatibility_mode="openai_responses"),
                    agent_reasoning=replace(config.agent_reasoning, enabled=False),
                )
                platform = RepoAutonomyPlatform(config)
                delivery = FakeRemoteDelivery()
                platform.remote_delivery = delivery
                platform.approve(
                    ApprovalScope.REMOTE_EXECUTION,
                    f"repair:{(ROOT / 'examples' / 'sample_repo').resolve()}",
                    "tester",
                    "allow local repair sandbox",
                )
                outcome = platform.repair_incident_file(
                    incident_path=ROOT / "examples" / "incidents" / "test_regression.json",
                    repo_root=ROOT / "examples" / "sample_repo",
                    max_attempts=1,
                    remote_delivery_confirmed=True,
                )

        self.assertTrue(outcome.success)
        self.assertTrue(outcome.final_validation.success)
        self.assertIsNotNone(outcome.commit_hash)
        self.assertEqual(outcome.delivery["status"], "created")
        self.assertEqual(delivery.calls[0]["commit_hash"], outcome.commit_hash)
        self.assertEqual(delivery.calls[0]["sandbox_path"], Path(outcome.sandbox_path))
        repaired_text = (Path(outcome.sandbox_path) / "app" / "service.py").read_text(encoding="utf-8")
        self.assertIn('"name"', repaired_text)
        self.assertIn("1 file changed", outcome.diff_summary)

    def test_remote_delivery_requires_explicit_confirmation(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_openai_server([build_fix_payload()]) as base_url:
            with temporary_env(OPENAI_API_KEY="test-key", OPENAI_BASE_URL=base_url):
                config = PlatformConfig.default(test_root)
                config = replace(
                    config,
                    openai=replace(config.openai, api_key="test-key", base_url=base_url, compatibility_mode="openai_responses"),
                    agent_reasoning=replace(config.agent_reasoning, enabled=False),
                )
                platform = RepoAutonomyPlatform(config)
                delivery = FakeRemoteDelivery()
                platform.remote_delivery = delivery
                outcome = platform.repair_incident_file(
                    incident_path=ROOT / "examples" / "incidents" / "test_regression.json",
                    repo_root=ROOT / "examples" / "sample_repo",
                    max_attempts=1,
                )

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.delivery["status"], "skipped")
        self.assertEqual(outcome.delivery["reason"], "remote_delivery_not_confirmed")
        self.assertTrue(outcome.delivery["requires_confirmation"])
        self.assertEqual(delivery.calls, [])

    def test_failed_validation_rolls_back_sandbox(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_openai_server([build_bad_payload()]) as base_url:
            with temporary_env(OPENAI_API_KEY="test-key", OPENAI_BASE_URL=base_url):
                config = PlatformConfig.default(test_root)
                config = replace(
                    config,
                    openai=replace(config.openai, api_key="test-key", base_url=base_url, compatibility_mode="openai_responses"),
                    agent_reasoning=replace(config.agent_reasoning, enabled=False),
                )
                platform = RepoAutonomyPlatform(config)
                platform.approve(
                    ApprovalScope.REMOTE_EXECUTION,
                    f"repair:{(ROOT / 'examples' / 'sample_repo').resolve()}",
                    "tester",
                    "allow local repair sandbox",
                )
                outcome = platform.repair_incident_file(
                    incident_path=ROOT / "examples" / "incidents" / "test_regression.json",
                    repo_root=ROOT / "examples" / "sample_repo",
                    max_attempts=1,
                )

        self.assertFalse(outcome.success)
        self.assertTrue(outcome.rollback_performed)
        self.assertIsNone(outcome.commit_hash)
        rolled_back_text = (Path(outcome.sandbox_path) / "app" / "service.py").read_text(encoding="utf-8")
        self.assertIn('"userName"', rolled_back_text)
        self.assertIn("rolled back", outcome.diff_summary.lower())

    def test_successful_repair_triggers_auto_dream_learning(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_openai_server([build_fix_payload()]) as base_url:
            with temporary_env(OPENAI_API_KEY="test-key", OPENAI_BASE_URL=base_url):
                config = PlatformConfig.default(test_root)
                config = replace(
                    config,
                    openai=replace(config.openai, api_key="test-key", base_url=base_url, compatibility_mode="openai_responses"),
                    agent_reasoning=replace(config.agent_reasoning, enabled=False),
                )
                platform = RepoAutonomyPlatform(config)
                platform.approve(
                    ApprovalScope.REMOTE_EXECUTION,
                    f"repair:{(ROOT / 'examples' / 'sample_repo').resolve()}",
                    "tester",
                    "allow local repair sandbox",
                )
                outcome = platform.repair_incident_file(
                    incident_path=ROOT / "examples" / "incidents" / "test_regression.json",
                    repo_root=ROOT / "examples" / "sample_repo",
                    max_attempts=1,
                )

        self.assertTrue(outcome.success)
        self.assertIn(outcome.trace.auto_learning_status, {"queued", "completed"})
        run_result = platform.run_learning_daemon_once()
        self.assertEqual(run_result["processed_jobs"], 1)
        self.assertTrue(platform.wait_for_learning_idle(1.0))
        stored_trace = platform.memory_store.trace_payload(outcome.trace.id)
        self.assertIsNotNone(stored_trace)
        self.assertEqual(stored_trace["auto_learning_status"], "completed")
        self.assertTrue(stored_trace["auto_learning_report"]["learned_skill_ids"])
        summary = platform.metrics_summary()
        self.assertEqual(summary["auto_dream_rate"], 1.0)
