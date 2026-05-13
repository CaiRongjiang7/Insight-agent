from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.config import GitHubActionsConfig
from repo_maintainer.github_actions import GitHubActionsClient
from repo_maintainer.models import (
    ApprovalScope,
    CompressedError,
    ContextBundle,
    IncidentReport,
    IncidentType,
    PreparedIncident,
    RepoSnapshot,
    SchedulerDecision,
)
from repo_maintainer.remote_delivery import RemoteDeliveryManager


def prepared_incident(metadata: dict[str, object], incident_id: str = "Jenkins Build #45") -> PreparedIncident:
    incident = IncidentReport(
        id=incident_id,
        title="CI failure",
        description="A CI job failed and needs an autonomous fix.",
        logs="FAILED tests/test_service.py",
        metadata=metadata,
    )
    return PreparedIncident(
        incident=incident,
        snapshot=RepoSnapshot(
            root_dir="",
            files=[],
            file_summaries={},
            dependency_edges=[],
            call_edges=[],
            language_breakdown={},
        ),
        compressed_error=CompressedError(
            error_type=IncidentType.TEST_REGRESSION,
            error_name="AssertionError",
            keywords=["test", "regression"],
            key_stack_frames=[],
            root_cause_cluster="service_contract",
            semantic_summary="The service response contract regressed.",
            confidence=0.9,
        ),
        matched_skills=[],
        relevant_files=[],
        context=ContextBundle(
            working_context={},
            short_term_memories=[],
            long_term_memories=[],
            matched_skills=[],
            relevant_files=[],
            budget_allocations={},
            semantic_focus=[],
        ),
        decision=SchedulerDecision(mode="single_agent", complexity_score=1, reasons=[], tasks=[], budget_breakdown={}),
        executions=[],
        repair_steps=[],
    )


class FakeSecurityManager:
    def __init__(self) -> None:
        self.write_calls: list[tuple[str, str, ApprovalScope]] = []

    def authorize_remote_write(self, channel: str, subject: str, *, scope: ApprovalScope = ApprovalScope.CI_WRITE):
        self.write_calls.append((channel, subject, scope))
        return []


class FakeGitHubClient:
    def __init__(self) -> None:
        self.pull_requests: list[dict[str, str]] = []

    def create_pull_request(self, owner: str, repo: str, title: str, head: str, base: str, body: str):
        payload = {"owner": owner, "repo": repo, "title": title, "head": head, "base": base, "body": body}
        self.pull_requests.append(payload)
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/1", "number": 1}


class FakeGitLabClient:
    def __init__(self) -> None:
        self.merge_requests: list[dict[str, str]] = []

    def create_merge_request(self, project_id: str, source_branch: str, target_branch: str, title: str, description: str):
        payload = {
            "project_id": project_id,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        self.merge_requests.append(payload)
        return {"web_url": f"https://gitlab.com/{project_id}/-/merge_requests/7", "iid": 7}


class RecordingDeliveryManager(RemoteDeliveryManager):
    def __init__(self, *args, auth_url: str = "https://remote.example/repo.git", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.auth_url = auth_url
        self.authenticated_requests: list[tuple[str, str, str]] = []
        self.pushes: list[dict[str, object]] = []

    def _authenticated_url(self, repo_url: str, *, provider: str, token: str) -> str:
        self.authenticated_requests.append((repo_url, provider, token))
        return self.auth_url

    def _push_branch(self, sandbox_path: Path, *, auth_url: str, branch: str) -> None:
        self.pushes.append({"sandbox_path": sandbox_path, "auth_url": auth_url, "branch": branch})


class RecordingGitHubClient(GitHubActionsClient):
    def __init__(self, security_manager) -> None:
        super().__init__(
            GitHubActionsConfig(
                token="test-token",
                token_env="GITHUB_TOKEN",
                api_url="https://api.github.com",
                api_version="2022-11-28",
                default_owner=None,
                default_repo=None,
                log_tail_chars=1000,
                enabled=True,
            ),
            security_manager,
        )
        self.endpoints: list[str] = []

    def _request_json(self, method: str, endpoint: str, payload=None, allow_empty_response: bool = False):
        self.endpoints.append(endpoint)
        return {"sha": "abc123"}


class FakeReadSecurityManager:
    def __init__(self) -> None:
        self.read_subjects: list[str] = []

    def authorize_github_read(self, subject: str):
        self.read_subjects.append(subject)
        return []


class RemoteDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = ROOT / ".test_runs" / f"remote-delivery-{uuid4().hex}"
        self.test_root.mkdir(parents=True, exist_ok=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_github_delivery_pushes_repair_branch_and_creates_pr(self) -> None:
        sandbox = self.test_root / "sandbox"
        commit_hash = "abcdef1234567890abcdef1234567890abcdef12"
        sandbox.mkdir()
        github = FakeGitHubClient()
        gitlab = FakeGitLabClient()
        security = FakeSecurityManager()
        manager = RecordingDeliveryManager(
            github_client=github,
            gitlab_client=gitlab,
            security_manager=security,
            github_token="gh-test-token",
            gitlab_token=None,
        )

        result = manager.deliver(
            prepared=prepared_incident(
                {
                    "repo_url": "https://github.com/octo/example.git",
                    "head_branch": "main",
                    "owner": "octo",
                    "repo": "example",
                }
            ),
            sandbox_path=sandbox,
            commit_hash=commit_hash,
            diff_summary="service.py | 2 +-",
            report_path="runtime/reports/trace.md",
        )

        branch = str(result["branch"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["provider"], "github")
        self.assertTrue(branch.startswith("agent/fix-jenkins-build-45-"))
        self.assertEqual(security.write_calls, [("github", "octo/example", ApprovalScope.GITHUB_WRITE)])
        self.assertEqual(manager.pushes, [{"sandbox_path": sandbox, "auth_url": "https://remote.example/repo.git", "branch": branch}])
        self.assertEqual(github.pull_requests[0]["head"], branch)
        self.assertEqual(github.pull_requests[0]["base"], "main")
        self.assertIn(commit_hash, github.pull_requests[0]["body"])

    def test_gitlab_delivery_pushes_repair_branch_and_creates_mr(self) -> None:
        sandbox = self.test_root / "sandbox"
        commit_hash = "1234567890abcdef1234567890abcdef12345678"
        sandbox.mkdir()
        github = FakeGitHubClient()
        gitlab = FakeGitLabClient()
        security = FakeSecurityManager()
        manager = RecordingDeliveryManager(
            github_client=github,
            gitlab_client=gitlab,
            security_manager=security,
            github_token=None,
            gitlab_token="gl-test-token",
        )

        result = manager.deliver(
            prepared=prepared_incident(
                {
                    "repo_url": "https://gitlab.com/group/project.git",
                    "project_id": "group/project",
                    "ref": "develop",
                },
                incident_id="gitlab-pipeline-77",
            ),
            sandbox_path=sandbox,
            commit_hash=commit_hash,
            diff_summary="service.py | 2 +-",
            report_path="runtime/reports/trace.md",
        )

        branch = str(result["branch"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["provider"], "gitlab")
        self.assertEqual(security.write_calls, [("gitlab", "group/project", ApprovalScope.CI_WRITE)])
        self.assertEqual(manager.pushes, [{"sandbox_path": sandbox, "auth_url": "https://remote.example/repo.git", "branch": branch}])
        self.assertEqual(gitlab.merge_requests[0]["source_branch"], branch)
        self.assertEqual(gitlab.merge_requests[0]["target_branch"], "develop")

    def test_push_branch_checks_out_repair_branch_before_pushing_ref(self) -> None:
        manager = RemoteDeliveryManager(
            github_client=FakeGitHubClient(),
            gitlab_client=FakeGitLabClient(),
            security_manager=FakeSecurityManager(),
            github_token="gh-test-token",
            gitlab_token="gl-test-token",
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("repo_maintainer.remote_delivery.subprocess.run", side_effect=fake_run):
            manager._push_branch(self.test_root / "sandbox", auth_url="https://github.com/octo/example.git", branch="agent/fix-test")

        self.assertEqual(calls[0][0], ["git", "checkout", "-B", "agent/fix-test"])
        self.assertEqual(
            calls[1][0],
            ["git", "push", "https://github.com/octo/example.git", "refs/heads/agent/fix-test:refs/heads/agent/fix-test"],
        )
        self.assertEqual(calls[0][1]["cwd"], self.test_root / "sandbox")
        self.assertEqual(calls[1][1]["cwd"], self.test_root / "sandbox")

    def test_ssh_remote_uses_token_https_url_for_push(self) -> None:
        manager = RemoteDeliveryManager(
            github_client=FakeGitHubClient(),
            gitlab_client=FakeGitLabClient(),
            security_manager=FakeSecurityManager(),
            github_token="gh-test-token",
            gitlab_token="gl-test-token",
        )

        github_url = manager._authenticated_url("git@github.com:octo/example.git", provider="github", token="gh-test-token")
        gitlab_url = manager._authenticated_url("ssh://git@gitlab.com/group/project.git", provider="gitlab", token="gl-test-token")

        self.assertEqual(github_url, "https://x-access-token:gh-test-token@github.com/octo/example.git")
        self.assertEqual(gitlab_url, "https://oauth2:gl-test-token@gitlab.com/group/project.git")

    def test_https_remote_trailing_slash_is_removed_from_authenticated_push_url(self) -> None:
        manager = RemoteDeliveryManager(
            github_client=FakeGitHubClient(),
            gitlab_client=FakeGitLabClient(),
            security_manager=FakeSecurityManager(),
            github_token="gh-test-token",
            gitlab_token="gl-test-token",
        )

        github_url = manager._authenticated_url(
            "https://github.com/second196/simple_repo.git/",
            provider="github",
            token="gh-test-token",
        )

        self.assertEqual(github_url, "https://x-access-token:gh-test-token@github.com/second196/simple_repo.git")

    def test_github_content_sha_quotes_paths_for_api_fallback(self) -> None:
        security = FakeReadSecurityManager()
        client = RecordingGitHubClient(security)

        sha = client.get_content_sha("octo", "example", "dir/file name.py", "agent/fix branch")

        self.assertEqual(sha, "abc123")
        self.assertEqual(security.read_subjects, ["octo/example"])
        self.assertEqual(client.endpoints, ["/repos/octo/example/contents/dir/file%20name.py?ref=agent%2Ffix%20branch"])


if __name__ == "__main__":
    unittest.main()
