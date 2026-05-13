from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.config import PlatformConfig
from repo_maintainer.models import ApprovalScope, IncidentReport, Skill
from repo_maintainer.platform import RepoAutonomyPlatform
from repo_maintainer.retrieval import SemanticRetriever
from repo_maintainer.sandbox import GitSandbox
from repo_maintainer.security import SecurityPolicyError


class FakeGitHubHandler(BaseHTTPRequestHandler):
    rerun_requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/repos/octo/example/actions/runs?"):
            payload = {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 101,
                        "workflow_id": 55,
                        "name": "CI",
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://github.com/octo/example/actions/runs/101",
                        "logs_url": "https://api.github.com/repos/octo/example/actions/runs/101/logs",
                        "rerun_url": "https://api.github.com/repos/octo/example/actions/runs/101/rerun",
                        "head_branch": "main",
                        "head_sha": "abc123",
                        "event": "push",
                        "created_at": "2026-04-19T14:00:00Z",
                        "updated_at": "2026-04-19T14:05:00Z",
                    }
                ],
            }
            self._json(payload)
            return
        if self.path == "/v1/repos/octo/example/actions/runs/101":
            self._json(
                {
                    "id": 101,
                    "workflow_id": 55,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "main",
                    "head_sha": "abc123",
                    "event": "push",
                }
            )
            return
        if self.path == "/v1/repos/octo/example/actions/runs/101/logs":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("job.txt", "Traceback: example failure\nFAILED tests/test_service.py")
            body = buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path in {
            "/v1/repos/octo/example/actions/runs/101/rerun",
            "/v1/repos/octo/example/actions/runs/101/rerun-failed-jobs",
        }:
            self.rerun_requests.append(self.path)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeOtherCiHandler(BaseHTTPRequestHandler):
    gitlab_retry_requests: list[str] = []
    jenkins_retry_requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/jenkins/api/json?tree=jobs[name,fullName,url,color,_class]":
            self._json(
                {
                    "jobs": [
                        {
                            "name": "sample",
                            "fullName": "sample",
                            "url": "http://jenkins/job/sample/",
                            "color": "red",
                            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                        },
                        {
                            "name": "test_repo",
                            "fullName": "test_repo",
                            "url": "http://jenkins/job/test_repo/",
                            "color": "blue",
                            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                        },
                    ]
                }
            )
            return
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
                        "id": 777,
                        "name": "CI",
                        "status": "failed",
                        "ref": "main",
                        "sha": "def456",
                        "web_url": "https://gitlab.example/pipelines/777",
                        "created_at": "2026-04-19T14:00:00Z",
                        "updated_at": "2026-04-19T14:05:00Z",
                    }
                ]
            )
            return
        if self.path == "/gitlab/projects/123/pipelines/777/jobs":
            self._json([{"id": 998, "name": "pytest", "status": "failed"}])
            return
        if self.path == "/gitlab/projects/123/jobs/998/trace":
            self._text("Traceback: gitlab failure\nFAILED tests/test_service.py")
            return
        if self.path.startswith("/jenkins/job/sample/api/json"):
            self._json(
                {
                    "builds": [
                        {"number": 45, "url": "http://jenkins/job/sample/45/", "result": "FAILURE", "timestamp": 2000},
                        {"number": 44, "url": "http://jenkins/job/sample/44/", "result": "FAILURE", "timestamp": 1000},
                    ]
                }
            )
            return
        if self.path == "/jenkins/job/sample/config.xml":
            self._text(
                """
<flow-definition>
  <definition>
    <script>
pipeline {
  agent any
  stages {
    stage('Test') {
      steps {
        sh 'python -m pytest -q'
      }
    }
  }
}
    </script>
  </definition>
  <scm>
    <userRemoteConfigs>
      <hudson.plugins.git.UserRemoteConfig>
        <url>https://github.com/octo/example.git</url>
      </hudson.plugins.git.UserRemoteConfig>
    </userRemoteConfigs>
  </scm>
</flow-definition>
                """.strip()
            )
            return
        if self.path.startswith("/jenkins/job/sample/45/api/json"):
            self._json(
                {
                    "actions": [
                        {
                            "remoteUrls": ["https://github.com/octo/example.git"],
                            "lastBuiltRevision": {
                                "SHA1": "ghi789",
                                "branch": [{"name": "origin/main", "SHA1": "ghi789"}],
                            },
                        }
                    ],
                    "changeSet": {"items": [{"commitId": "ghi789"}]},
                }
            )
            return
        if self.path == "/jenkins/job/sample/45/consoleText":
            self._text("Traceback: jenkins failure\nFAILED tests/test_service.py")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        if self.path == "/gitlab/projects/123/pipelines/777/retry":
            self.gitlab_retry_requests.append(self.path)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/jenkins/job/sample/45/rebuild":
            self.jenkins_retry_requests.append(self.path)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
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
def fake_github_server():
    FakeGitHubHandler.rerun_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def fake_other_ci_server():
    FakeOtherCiHandler.gitlab_retry_requests = []
    FakeOtherCiHandler.jenkins_retry_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOtherCiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "gitlab": f"http://127.0.0.1:{server.server_address[1]}/gitlab",
            "jenkins": f"http://127.0.0.1:{server.server_address[1]}/jenkins",
        }
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


class ExtendedPlatformTests(unittest.TestCase):
    def test_repo_indexer_builds_symbol_graph(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
        snapshot = platform.indexer.index(ROOT / "examples" / "sample_repo")
        symbol_names = {symbol.name for symbol in snapshot.symbols}
        self.assertIn("get_user_summary", symbol_names)
        self.assertIsNotNone(snapshot.graph_metadata)
        self.assertGreater(snapshot.graph_metadata.graph_nodes, 0)

    def test_semantic_retriever_reranks_dependency_skill(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
        retriever = SemanticRetriever(platform.config.retrieval, platform.embedding_client)
        skills = [
            Skill(
                id="dep",
                name="Dependency Pin Reconcile",
                description="Resolve dependency conflicts and version pin mismatches.",
                triggers=["resolutionimpossible", "dependency", "requirements"],
                error_types=["dependency_conflict"],
                action_template="Align pinned dependencies.",
            ),
            Skill(
                id="test",
                name="Regression Guard",
                description="Restore test expectations after behavior regressions.",
                triggers=["assertionerror", "test"],
                error_types=["test_regression"],
                action_template="Fix regressions.",
            ),
        ]
        matches = retriever.rank_skills("dependency conflict resolution impossible requirements.txt", skills, limit=2)
        self.assertEqual(matches[0].item_id, "dep")
        self.assertGreaterEqual(matches[0].reranker_score, 0.0)
        self.assertIn(platform.vector_store.backend_name, {"sqlite_ann", "faiss_flat", "faiss_hnsw", "hnswlib", "milvus", "pgvector"})

    def test_repo_indexer_emits_data_flow_edges(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
        snapshot = platform.indexer.index(ROOT / "examples" / "sample_repo")
        self.assertTrue(any(edge.edge_type == "data_flow" for edge in snapshot.graph_edges))

    def test_github_sync_downloads_logs_and_persists_incident(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_github_server() as api_url:
            with temporary_env(GITHUB_TOKEN="test-token", GITHUB_API_URL=api_url, GITHUB_REPOSITORY="octo/example"):
                platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
                sync = platform.sync_github_failures(limit=1)

        self.assertEqual(len(sync.runs), 1)
        self.assertEqual(sync.runs[0].run_id, 101)
        self.assertEqual(len(sync.incidents), 1)
        self.assertIn("FAILED tests/test_service.py", sync.incidents[0].logs)
        incident_file = test_root / "runtime" / "incidents" / f"{sync.incidents[0].id}.json"
        self.assertTrue(incident_file.exists())

    def test_github_rerun_requires_approval(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_github_server() as api_url:
            with temporary_env(GITHUB_TOKEN="test-token", GITHUB_API_URL=api_url, GITHUB_REPOSITORY="octo/example"):
                platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
                with self.assertRaises(SecurityPolicyError):
                    platform.rerun_github_workflow(101, owner="octo", repo="example")
                platform.approve(ApprovalScope.GITHUB_WRITE, "octo/example", "tester", "allow rerun")
                result = platform.rerun_github_workflow(101, owner="octo", repo="example", failed_only=True)

        self.assertEqual(result["run_id"], 101)
        self.assertEqual(FakeGitHubHandler.rerun_requests[-1], "/v1/repos/octo/example/actions/runs/101/rerun-failed-jobs")

    def test_ci_sync_supports_gitlab_and_jenkins(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_other_ci_server() as endpoints:
            with temporary_env(
                GITLAB_TOKEN="test-token",
                GITLAB_API_URL=endpoints["gitlab"],
                GITLAB_PROJECT_ID="123",
                JENKINS_BASE_URL=endpoints["jenkins"],
                JENKINS_JOB_NAME="sample",
            ):
                config = PlatformConfig.default(test_root)
                config = replace(config, gitlab=replace(config.gitlab, token="test-token", enabled=True))
                platform = RepoAutonomyPlatform(config)
                gitlab_sync = platform.sync_ci_failures("gitlab_ci", limit=1)
                jenkins_sync = platform.sync_ci_failures("jenkins", limit=1)

        self.assertEqual(gitlab_sync.provider, "gitlab_ci")
        self.assertEqual(jenkins_sync.provider, "jenkins")
        self.assertIn("FAILED tests/test_service.py", gitlab_sync.incidents[0].logs)
        self.assertIn("FAILED tests/test_service.py", jenkins_sync.incidents[0].logs)
        self.assertEqual(jenkins_sync.runs[0].run_id, "45")
        self.assertEqual(gitlab_sync.incidents[0].metadata["repo_url"], "https://gitlab.example/octo/example.git")
        self.assertEqual(jenkins_sync.incidents[0].metadata["repo_url"], "https://github.com/octo/example.git")
        self.assertEqual(jenkins_sync.incidents[0].metadata["head_branch"], "main")
        self.assertEqual(jenkins_sync.incidents[0].metadata["head_sha"], "ghi789")
        self.assertEqual(jenkins_sync.incidents[0].reported_at, datetime.fromtimestamp(2, tz=timezone.utc).isoformat())

    def test_jenkins_jobs_can_be_listed(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_other_ci_server() as endpoints:
            with temporary_env(JENKINS_BASE_URL=endpoints["jenkins"], JENKINS_JOB_NAME="sample"):
                platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
                jobs = platform.list_jenkins_jobs()

        self.assertEqual([job["full_name"] for job in jobs], ["sample", "test_repo"])

    def test_ci_rerun_supports_gitlab_and_jenkins_with_ci_write_approval(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        with fake_other_ci_server() as endpoints:
            with temporary_env(
                GITLAB_TOKEN="test-token",
                GITLAB_API_URL=endpoints["gitlab"],
                GITLAB_PROJECT_ID="123",
                JENKINS_BASE_URL=endpoints["jenkins"],
                JENKINS_JOB_NAME="sample",
            ):
                config = PlatformConfig.default(test_root)
                config = replace(config, gitlab=replace(config.gitlab, token="test-token", enabled=True))
                platform = RepoAutonomyPlatform(config)
                with self.assertRaises(SecurityPolicyError):
                    platform.rerun_ci_workflow("gitlab_ci", run_id="777", project_id="123")
                platform.approve(ApprovalScope.CI_WRITE, "123", "tester", "allow gitlab rerun")
                gitlab_result = platform.rerun_ci_workflow("gitlab_ci", run_id="777", project_id="123")
                with self.assertRaises(SecurityPolicyError):
                    platform.rerun_ci_workflow("jenkins", run_id="45", job_name="sample")
                platform.approve(ApprovalScope.CI_WRITE, "sample", "tester", "allow jenkins rerun")
                jenkins_result = platform.rerun_ci_workflow("jenkins", run_id="45", job_name="sample")

        self.assertEqual(gitlab_result["run_id"], "777")
        self.assertEqual(jenkins_result["run_id"], "45")
        self.assertEqual(FakeOtherCiHandler.gitlab_retry_requests[-1], "/gitlab/projects/123/pipelines/777/retry")
        self.assertEqual(FakeOtherCiHandler.jenkins_retry_requests[-1], "/jenkins/job/sample/45/rebuild")

    def test_container_sandbox_falls_back_when_runtime_missing(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        config = PlatformConfig.default(test_root)
        config = replace(
            config,
            sandbox=replace(config.sandbox, backend="container", container_runtime="definitely-missing-runtime"),
        )
        sandbox = GitSandbox.create(ROOT / "examples" / "sample_repo", config.paths.sandboxes_dir, "sandbox-case", config.sandbox)
        self.assertEqual(sandbox.runtime_backend, "workspace")
        self.assertTrue(sandbox.degraded_reason)

    def test_prepare_incident_uses_thread_runtime_backend(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        config = PlatformConfig.default(test_root)
        config = replace(config, runtime=replace(config.runtime, backend="thread"))
        platform = RepoAutonomyPlatform(config)
        incident = IncidentReport(
            id="cross-runtime",
            title="Cross module defect",
            description="service and repository interaction failed",
            logs='Traceback (most recent call last):\n  File "app/service.py", line 9, in run\n  File "app/repository.py", line 4, in fetch\nAssertionError\n',
            changed_files=["app/service.py", "app/repository.py", "tests/test_service.py"],
        )
        prepared = platform.prepare_incident(incident, repo_root=ROOT / "examples" / "sample_repo")
        self.assertEqual(prepared.decision.mode, "multi_agent")
        self.assertGreaterEqual(len(prepared.executions), 6)
        self.assertTrue(all(execution.metadata.get("runtime_backend") for execution in prepared.executions))
        self.assertTrue(any(execution.role.value == "critic" for execution in prepared.executions))
        self.assertTrue(any(execution.role.value == "rebuttal" for execution in prepared.executions))
        self.assertTrue(any(execution.role.value == "reflect" for execution in prepared.executions))
        self.assertTrue(all(execution.metadata.get("sandbox_profile") for execution in prepared.executions))
        self.assertTrue(any(execution.metadata.get("tool_permissions") for execution in prepared.executions))
        self.assertTrue(any(execution.metadata.get("tool_results") for execution in prepared.executions))
        self.assertTrue(any(any(item.get("tool_name") == "sandbox.exec" for item in execution.metadata.get("tool_results", [])) for execution in prepared.executions))
        self.assertTrue(any(execution.metadata.get("blackboard_reads", 0) >= 1 for execution in prepared.executions if execution.role.value != "diagnose"))
        snapshot = platform.agent_runtime.collaboration_snapshot(prepared.executions)
        self.assertGreaterEqual(snapshot.get("debate_rounds", 0), 2)
        self.assertTrue(snapshot.get("message_log"))
        self.assertTrue(snapshot.get("tool_calls"))

    def test_analyze_incident_persists_collaboration_memories(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        platform = RepoAutonomyPlatform(PlatformConfig.default(test_root))
        incident = IncidentReport(
            id="cross-memory",
            title="Cross module defect",
            description="service and repository interaction failed",
            logs='Traceback (most recent call last):\n  File "app/service.py", line 9, in run\n  File "app/repository.py", line 4, in fetch\nAssertionError\n',
            changed_files=["app/service.py", "app/repository.py", "tests/test_service.py"],
        )
        trace = platform.analyze_incident(incident, repo_root=ROOT / "examples" / "sample_repo")
        memories = platform.memory_store.related_collaboration_memories(
            incident_type=trace.compressed_error.error_type.value,
            cluster=trace.compressed_error.root_cause_cluster,
            keywords=trace.compressed_error.keywords,
            limit=10,
        )
        self.assertGreaterEqual(trace.collaboration_blackboard.get("debate_rounds", 0), 2)
        self.assertTrue(trace.collaboration_blackboard.get("message_log"))
        self.assertTrue(any(memory.get("role") == "critic" for memory in memories))
        self.assertTrue(any(memory.get("role") == "rebuttal" for memory in memories))
        self.assertTrue(any(memory.get("category") == "consensus" for memory in memories))
