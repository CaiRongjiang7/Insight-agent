from __future__ import annotations

import io
import base64
import json
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import GitHubActionsConfig
from .models import GitHubSyncResult, GitHubWorkflowRun, IncidentReport
from .security import SecurityManager


class GitHubActionsError(RuntimeError):
    pass


class GitHubActionsClient:
    def __init__(self, config: GitHubActionsConfig, security_manager: SecurityManager) -> None:
        self.config = config
        self.security_manager = security_manager

    def is_configured(self) -> bool:
        return self.config.enabled and bool(self.config.token)

    def list_workflow_runs(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        status: str | None = None,
        per_page: int = 10,
    ) -> list[GitHubWorkflowRun]:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_read(subject)
        query = {"per_page": per_page}
        if branch:
            query["branch"] = branch
        if status:
            query["status"] = status
        endpoint = f"/repos/{owner}/{repo}/actions/runs?{urlencode(query)}"
        payload = self._request_json("GET", endpoint)
        runs = payload.get("workflow_runs", [])
        return [self._parse_run(item) for item in runs if isinstance(item, dict)]

    def get_workflow_run(self, owner: str, repo: str, run_id: int) -> GitHubWorkflowRun:
        subject = f"{owner}/{repo}/runs/{run_id}"
        self.security_manager.authorize_github_read(subject)
        payload = self._request_json("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")
        return self._parse_run(payload)

    def download_workflow_run_logs(self, owner: str, repo: str, run_id: int) -> str:
        subject = f"{owner}/{repo}/runs/{run_id}/logs"
        self.security_manager.authorize_github_read(subject)
        raw = self._request_bytes("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs")
        if raw.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                texts = []
                for name in sorted(archive.namelist()):
                    if name.endswith("/"):
                        continue
                    texts.append(archive.read(name).decode("utf-8", errors="ignore"))
            return "\n\n".join(texts)[-self.config.log_tail_chars :]
        return raw.decode("utf-8", errors="ignore")[-self.config.log_tail_chars :]

    def rerun_workflow_run(self, owner: str, repo: str, run_id: int, failed_only: bool = False) -> None:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_write(subject)
        suffix = "rerun-failed-jobs" if failed_only else "rerun"
        self._request_json(
            "POST",
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/{suffix}",
            payload={"enable_debug_logging": False},
            allow_empty_response=True,
        )

    def create_pull_request(self, owner: str, repo: str, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_write(subject)
        return self._request_json(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            payload={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "maintainer_can_modify": True,
            },
        )

    def get_ref_sha(self, owner: str, repo: str, ref: str) -> str:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_read(subject)
        payload = self._request_json("GET", f"/repos/{owner}/{repo}/git/ref/heads/{ref}")
        sha = payload.get("object", {}).get("sha") if isinstance(payload.get("object"), dict) else None
        if not sha:
            raise GitHubActionsError(f"GitHub ref `{ref}` did not include an object SHA.")
        return str(sha)

    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_write(subject)
        try:
            return self._request_json(
                "POST",
                f"/repos/{owner}/{repo}/git/refs",
                payload={"ref": f"refs/heads/{branch}", "sha": sha},
            )
        except GitHubActionsError as exc:
            if "Reference already exists" in str(exc) or "already_exists" in str(exc):
                return self._request_json("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
            raise

    def get_content_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_read(subject)
        try:
            payload = self._request_json("GET", f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
        except GitHubActionsError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        sha = payload.get("sha") if isinstance(payload, dict) else None
        return str(sha) if sha else None

    def upsert_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        branch: str,
        message: str,
        content: bytes,
        sha: str | None,
    ) -> dict[str, Any]:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_write(subject)
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return self._request_json("PUT", f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}", payload=payload)

    def delete_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        branch: str,
        message: str,
        sha: str,
    ) -> dict[str, Any]:
        subject = f"{owner}/{repo}"
        self.security_manager.authorize_github_write(subject)
        return self._request_json(
            "DELETE",
            f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
            payload={"message": message, "sha": sha, "branch": branch},
        )

    def sync_failed_runs(
        self,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        limit: int = 5,
    ) -> GitHubSyncResult:
        resolved_owner = owner or self.config.default_owner
        resolved_repo = repo or self.config.default_repo
        if not resolved_owner or not resolved_repo:
            raise GitHubActionsError("GitHub owner/repo is required. Set GITHUB_REPOSITORY or pass both values.")
        runs = self.list_workflow_runs(resolved_owner, resolved_repo, branch=branch, status="completed", per_page=limit * 3)
        failed_runs = [run for run in runs if run.conclusion in {"failure", "cancelled", "timed_out", "startup_failure"}][:limit]
        incidents: list[IncidentReport] = []
        latest_logs_excerpt = ""
        for run in failed_runs:
            logs = self.download_workflow_run_logs(resolved_owner, resolved_repo, run.run_id)
            latest_logs_excerpt = latest_logs_excerpt or logs[: self.config.log_tail_chars]
            incidents.append(self.incident_from_run(resolved_owner, resolved_repo, run, logs))
        return GitHubSyncResult(
            owner=resolved_owner,
            repo=resolved_repo,
            runs=failed_runs,
            incidents=incidents,
            latest_logs_excerpt=latest_logs_excerpt,
        )

    def incident_from_run(self, owner: str, repo: str, run: GitHubWorkflowRun, logs: str) -> IncidentReport:
        return IncidentReport(
            id=f"github-run-{run.run_id}",
            title=f"GitHub Actions failure: {run.name}",
            description=(
                f"Workflow run {run.run_id} in {owner}/{repo} concluded with {run.conclusion or run.status}. "
                f"Branch: {run.head_branch or 'unknown'}. Event: {run.event or 'unknown'}."
            ),
            logs=logs,
            changed_files=[],
            suspected_modules=[],
            metadata={
                "provider": "github_actions",
                "owner": owner,
                "repo": repo,
                "repo_url": f"https://github.com/{owner}/{repo}.git",
                "run_id": run.run_id,
                "workflow_id": run.workflow_id,
                "html_url": run.html_url,
                "head_branch": run.head_branch,
                "head_sha": run.head_sha,
                "ref": run.head_branch,
                "sha": run.head_sha,
                "validation_commands": [],
            },
        )

    def _parse_run(self, payload: dict[str, Any]) -> GitHubWorkflowRun:
        return GitHubWorkflowRun(
            run_id=int(payload.get("id", 0)),
            workflow_id=int(payload["workflow_id"]) if payload.get("workflow_id") is not None else None,
            name=str(payload.get("name", "workflow-run")),
            status=str(payload.get("status", "")),
            conclusion=payload.get("conclusion"),
            html_url=payload.get("html_url"),
            logs_url=payload.get("logs_url"),
            rerun_url=payload.get("rerun_url"),
            head_branch=payload.get("head_branch"),
            head_sha=payload.get("head_sha"),
            event=payload.get("event"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    def _request_json(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        allow_empty_response: bool = False,
    ) -> dict[str, Any]:
        raw = self._request_bytes(method, endpoint, payload=payload, allow_empty_response=allow_empty_response)
        if not raw and allow_empty_response:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise GitHubActionsError(f"GitHub API returned malformed JSON for {endpoint}.") from exc
        if not isinstance(data, dict):
            raise GitHubActionsError(f"GitHub API returned unexpected payload for {endpoint}.")
        return data

    def _request_bytes(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        allow_empty_response: bool = False,
    ) -> bytes:
        if not self.is_configured():
            raise GitHubActionsError(
                f"Missing GitHub token in environment variable `{self.config.token_env}`. "
                "Set it before using remote GitHub Actions integration."
            )
        last_error: Exception | None = None
        for _ in range(2):
            url = self.config.api_url.rstrip("/") + endpoint
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": self.config.api_version,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=120) as response:
                    return response.read()
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                if allow_empty_response and exc.code in {201, 202, 204}:
                    return b""
                raise GitHubActionsError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
            except (URLError, OSError) as exc:
                last_error = exc
                continue
        raise GitHubActionsError(f"Failed to reach GitHub API: {last_error}")
