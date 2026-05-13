from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .config import GitLabCiConfig
from .models import CiSyncResult, CiWorkflowRun, IncidentReport
from .security import SecurityManager


class GitLabCiError(RuntimeError):
    pass


class GitLabCiClient:
    def __init__(self, config: GitLabCiConfig, security_manager: SecurityManager) -> None:
        self.config = config
        self.security_manager = security_manager

    def is_configured(self) -> bool:
        return self.config.enabled and bool(self.config.token)

    def list_failed_pipelines(self, project_id: str, ref: str | None = None, per_page: int = 10) -> list[CiWorkflowRun]:
        self.security_manager.authorize_remote_read("gitlab", project_id)
        query: dict[str, Any] = {"status": "failed", "per_page": per_page}
        if ref:
            query["ref"] = ref
        payload = self._request_json("GET", f"/projects/{quote(project_id, safe='')}/pipelines?{urlencode(query)}")
        if not isinstance(payload, list):
            raise GitLabCiError("GitLab API returned malformed pipeline list.")
        runs: list[CiWorkflowRun] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            runs.append(
                CiWorkflowRun(
                    provider="gitlab_ci",
                    run_id=str(item.get("id", "")),
                    name=str(item.get("name") or item.get("ref") or "pipeline"),
                    status=str(item.get("status", "")),
                    conclusion=str(item.get("status", "")),
                    web_url=item.get("web_url"),
                    logs_url=None,
                    rerun_target=f"/projects/{quote(project_id, safe='')}/pipelines/{item.get('id')}/retry",
                    ref=item.get("ref"),
                    revision=item.get("sha"),
                    created_at=item.get("created_at"),
                    updated_at=item.get("updated_at"),
                    metadata={"project_id": project_id},
                )
            )
        return runs

    def get_project_details(self, project_id: str) -> dict[str, Any]:
        self.security_manager.authorize_remote_read("gitlab", f"{project_id}/project")
        payload = self._request_json("GET", f"/projects/{quote(project_id, safe='')}")
        if not isinstance(payload, dict):
            raise GitLabCiError("GitLab API returned malformed project metadata.")
        return payload

    def download_pipeline_logs(self, project_id: str, pipeline_id: str) -> str:
        self.security_manager.authorize_remote_read("gitlab", f"{project_id}/pipelines/{pipeline_id}")
        jobs = self._request_json("GET", f"/projects/{quote(project_id, safe='')}/pipelines/{pipeline_id}/jobs")
        if not isinstance(jobs, list):
            return ""
        traces: list[str] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if str(job.get("status", "")) not in {"failed", "canceled"}:
                continue
            job_id = str(job.get("id", ""))
            if not job_id:
                continue
            trace = self._request_bytes("GET", f"/projects/{quote(project_id, safe='')}/jobs/{job_id}/trace").decode("utf-8", errors="ignore")
            traces.append(f"[job:{job.get('name', job_id)}]\n{trace}")
            if len(traces) >= 3:
                break
        return "\n\n".join(traces)[-self.config.log_tail_chars :]

    def rerun_pipeline(self, project_id: str, pipeline_id: str) -> None:
        self.security_manager.authorize_remote_write("gitlab", project_id)
        self._request_json(
            "POST",
            f"/projects/{quote(project_id, safe='')}/pipelines/{pipeline_id}/retry",
            payload={},
            allow_empty_response=True,
        )

    def create_merge_request(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        self.security_manager.authorize_remote_write("gitlab", project_id)
        payload = self._request_json(
            "POST",
            f"/projects/{quote(project_id, safe='')}/merge_requests",
            payload={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "remove_source_branch": False,
            },
        )
        if not isinstance(payload, dict):
            raise GitLabCiError("GitLab API returned malformed merge request payload.")
        return payload

    def sync_failed_runs(self, project_id: str | None = None, ref: str | None = None, limit: int = 5) -> CiSyncResult:
        resolved_project_id = project_id or self.config.default_project_id
        if not resolved_project_id:
            raise GitLabCiError("GitLab project id is required.")
        project_details = self.get_project_details(resolved_project_id)
        repo_url = str(project_details.get("http_url_to_repo") or project_details.get("web_url") or "").strip()
        runs = self.list_failed_pipelines(resolved_project_id, ref=ref, per_page=limit)[:limit]
        incidents: list[IncidentReport] = []
        latest_logs_excerpt = ""
        for run in runs:
            run.metadata.update(
                {
                    "project_id": resolved_project_id,
                    "repo_url": repo_url,
                    "default_branch": project_details.get("default_branch"),
                }
            )
            logs = self.download_pipeline_logs(resolved_project_id, run.run_id)
            latest_logs_excerpt = latest_logs_excerpt or logs[: self.config.log_tail_chars]
            incidents.append(
                IncidentReport(
                    id=f"gitlab-pipeline-{run.run_id}",
                    title=f"GitLab CI failure: {run.name}",
                    description=f"Pipeline {run.run_id} in project {resolved_project_id} failed on ref {run.ref or 'unknown'}.",
                    logs=logs,
                    changed_files=[],
                    suspected_modules=[],
                    metadata={
                        "provider": "gitlab_ci",
                        "project_id": resolved_project_id,
                        "pipeline_id": run.run_id,
                        "ref": run.ref,
                        "sha": run.revision,
                        "repo_url": repo_url,
                        "default_branch": project_details.get("default_branch"),
                        "web_url": project_details.get("web_url"),
                    },
                )
            )
        return CiSyncResult(provider="gitlab_ci", runs=runs, incidents=incidents, latest_logs_excerpt=latest_logs_excerpt)

    def _request_json(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_empty_response: bool = False,
    ) -> Any:
        raw = self._request_bytes(method, endpoint, payload=payload, allow_empty_response=allow_empty_response)
        if not raw and allow_empty_response:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _request_bytes(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_empty_response: bool = False,
    ) -> bytes:
        if not self.is_configured():
            raise GitLabCiError(f"Missing GitLab token in `{self.config.token_env}`.")
        request = Request(
            self.config.api_url.rstrip("/") + endpoint,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={
                "PRIVATE-TOKEN": str(self.config.token),
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
            raise GitLabCiError(f"GitLab API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitLabCiError(f"Failed to reach GitLab API: {exc}") from exc
