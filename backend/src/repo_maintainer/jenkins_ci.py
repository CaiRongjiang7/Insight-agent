from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .config import JenkinsConfig
from .models import CiSyncResult, CiWorkflowRun, IncidentReport, iso_now
from .security import SecurityManager


class JenkinsCiError(RuntimeError):
    pass


class JenkinsCiClient:
    FILE_REFERENCE = re.compile(
        r"(?:(?:File\s+\"(?P<quoted>[^\"]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|txt|md))\")|"
        r"(?P<plain>[A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|txt|md))"
        r"(?::\d+|::[A-Za-z_][A-Za-z0-9_]*|\s|$))",
        re.IGNORECASE,
    )

    def __init__(self, config: JenkinsConfig, security_manager: SecurityManager) -> None:
        self.config = config
        self.security_manager = security_manager

    def is_configured(self) -> bool:
        return self.config.enabled and bool(self.config.base_url)

    def list_jobs(self) -> list[dict[str, str | None]]:
        self.security_manager.authorize_remote_read("jenkins", "jobs")
        items = self._collect_jobs()
        unique: dict[str, dict[str, str | None]] = {}
        for item in items:
            full_name = str(item.get("full_name") or item.get("name") or "").strip()
            if not full_name:
                continue
            unique[full_name] = item
        return sorted(unique.values(), key=lambda item: str(item.get("full_name") or item.get("name") or "").lower())

    def list_failed_builds(self, job_name: str, limit: int = 10) -> list[CiWorkflowRun]:
        self.security_manager.authorize_remote_read("jenkins", job_name)
        payload = self._request_json("GET", f"{self._job_path(job_name)}/api/json?tree=builds[number,url,result,timestamp,id]") or {}
        builds = payload.get("builds", []) if isinstance(payload, dict) else []
        builds = sorted(
            [item for item in builds if isinstance(item, dict)],
            key=lambda item: int(item.get("timestamp") or 0),
            reverse=True,
        )
        runs: list[CiWorkflowRun] = []
        for item in builds[: limit * 3]:
            result = str(item.get("result", ""))
            if result not in {"FAILURE", "ABORTED", "UNSTABLE"}:
                continue
            build_number = str(item.get("number", ""))
            created_at = self._timestamp_to_iso(item.get("timestamp"))
            runs.append(
                CiWorkflowRun(
                    provider="jenkins",
                    run_id=build_number,
                    name=job_name,
                    status=result.lower(),
                    conclusion=result.lower(),
                    web_url=item.get("url"),
                    logs_url=f"{self._job_path(job_name)}/{build_number}/consoleText",
                    rerun_target=f"{self._job_path(job_name)}/{build_number}/rebuild",
                    ref=None,
                    revision=None,
                    created_at=created_at,
                    updated_at=created_at,
                    metadata={"job_name": job_name, "timestamp": item.get("timestamp")},
                )
            )
            if len(runs) >= limit:
                break
        return runs

    def get_job_config_xml(self, job_name: str) -> str:
        self.security_manager.authorize_remote_read("jenkins", f"{job_name}/config")
        raw = self._request_bytes("GET", f"{self._job_path(job_name)}/config.xml")
        return raw.decode("utf-8", errors="ignore")

    def get_build_metadata(self, job_name: str, build_number: str) -> dict[str, Any]:
        self.security_manager.authorize_remote_read("jenkins", f"{job_name}/{build_number}/metadata")
        payload = self._request_json(
            "GET",
            (
                f"{self._job_path(job_name)}/{build_number}/api/json"
                "?tree=actions[parameters[name,value],lastBuiltRevision[SHA1,branch[name,SHA1]],remoteUrls],"
                "changeSet[items[commitId]],url,result,id"
            ),
        )
        if not isinstance(payload, dict):
            raise JenkinsCiError("Jenkins build metadata payload is malformed.")
        return self._parse_build_metadata(payload)

    def download_build_logs(self, job_name: str, build_number: str) -> str:
        self.security_manager.authorize_remote_read("jenkins", f"{job_name}/{build_number}")
        logs = self._request_bytes("GET", f"{self._job_path(job_name)}/{build_number}/consoleText").decode("utf-8", errors="ignore")
        return logs[-self.config.log_tail_chars :]

    def rerun_build(self, job_name: str, build_number: str) -> None:
        self.security_manager.authorize_remote_write("jenkins", job_name)
        self._request_json(
            "POST",
            f"{self._job_path(job_name)}/{build_number}/rebuild",
            payload={},
            allow_empty_response=True,
        )

    def sync_failed_runs(self, job_name: str | None = None, limit: int = 5) -> CiSyncResult:
        resolved_job = job_name or self.config.default_job
        if not resolved_job:
            raise JenkinsCiError("Jenkins job name is required.")
        try:
            job_config = self._parse_job_config(self.get_job_config_xml(resolved_job))
        except JenkinsCiError:
            job_config = {"repo_url": None, "validation_commands": []}
        runs = self.list_failed_builds(resolved_job, limit=limit)
        incidents: list[IncidentReport] = []
        latest_logs_excerpt = ""
        for run in runs:
            try:
                build_metadata = self.get_build_metadata(resolved_job, run.run_id)
            except JenkinsCiError:
                build_metadata = {"repo_url": None, "branch": None, "revision": None}
            repo_url = str(build_metadata.get("repo_url") or job_config.get("repo_url") or "").strip()
            branch = str(build_metadata.get("branch") or job_config.get("branch") or "").strip() or None
            revision = str(build_metadata.get("revision") or "").strip() or None
            validation_commands = list(job_config.get("validation_commands", []))
            run.ref = branch
            run.revision = revision
            run.metadata.update(
                {
                    "job_name": resolved_job,
                    "repo_url": repo_url,
                    "branch": branch,
                    "revision": revision,
                    "validation_commands": validation_commands,
                }
            )
            logs = self.download_build_logs(resolved_job, run.run_id)
            log_files = self._extract_file_references(logs)
            latest_logs_excerpt = latest_logs_excerpt or logs[: self.config.log_tail_chars]
            incidents.append(
                IncidentReport(
                    id=f"jenkins-build-{run.run_id}",
                    title=f"Jenkins failure: {resolved_job} #{run.run_id}",
                    description=f"Jenkins build {run.run_id} for job {resolved_job} finished with {run.conclusion}.",
                    logs=logs,
                    changed_files=log_files,
                    suspected_modules=[],
                    reported_at=run.created_at or iso_now(),
                    metadata={
                        "provider": "jenkins",
                        "job_name": resolved_job,
                        "build_number": run.run_id,
                        "repo_url": repo_url,
                        "head_branch": branch,
                        "ref": branch,
                        "head_sha": revision,
                        "sha": revision,
                        "validation_commands": validation_commands,
                    },
                )
            )
        return CiSyncResult(provider="jenkins", runs=runs, incidents=incidents, latest_logs_excerpt=latest_logs_excerpt)

    def refresh_incident(self, incident: IncidentReport) -> IncidentReport:
        metadata = dict(incident.metadata)
        job_name = str(metadata.get("job_name") or self.config.default_job or "").strip()
        build_number = str(metadata.get("build_number") or metadata.get("run_id") or self._build_number_from_incident(incident)).strip()
        if not job_name or not build_number:
            raise JenkinsCiError("Jenkins incident is missing `job_name` or `build_number`; cannot refresh SCM metadata.")

        try:
            job_config = self._parse_job_config(self.get_job_config_xml(job_name))
        except JenkinsCiError:
            job_config = {"repo_url": None, "validation_commands": []}
        try:
            build_metadata = self.get_build_metadata(job_name, build_number)
        except JenkinsCiError:
            build_metadata = {"repo_url": None, "branch": None, "revision": None}

        repo_url = str(build_metadata.get("repo_url") or job_config.get("repo_url") or metadata.get("repo_url") or "").strip()
        branch = str(build_metadata.get("branch") or job_config.get("branch") or metadata.get("head_branch") or metadata.get("ref") or "").strip() or None
        revision = str(build_metadata.get("revision") or metadata.get("head_sha") or metadata.get("sha") or "").strip() or None
        validation_commands = list(job_config.get("validation_commands") or metadata.get("validation_commands") or [])
        logs = self.download_build_logs(job_name, build_number)
        changed_files = self._extract_file_references(logs) or list(incident.changed_files)
        refreshed_metadata = {
            **metadata,
            "provider": "jenkins",
            "job_name": job_name,
            "build_number": build_number,
            "repo_url": repo_url,
            "head_branch": branch,
            "ref": branch,
            "head_sha": revision,
            "sha": revision,
            "validation_commands": validation_commands,
        }
        if build_metadata.get("web_url"):
            refreshed_metadata["web_url"] = build_metadata.get("web_url")
        if build_metadata.get("result"):
            refreshed_metadata["result"] = build_metadata.get("result")
        return IncidentReport(
            id=incident.id,
            title=incident.title or f"Jenkins failure: {job_name} #{build_number}",
            description=incident.description or f"Jenkins build {build_number} for job {job_name} failed.",
            logs=logs or incident.logs,
            changed_files=changed_files,
            suspected_modules=list(incident.suspected_modules),
            metadata=refreshed_metadata,
            reported_at=incident.reported_at,
        )

    def _collect_jobs(self, job_name: str | None = None) -> list[dict[str, str | None]]:
        endpoint = "/api/json?tree=jobs[name,fullName,url,color,_class]" if not job_name else f"{self._job_path(job_name)}/api/json?tree=jobs[name,fullName,url,color,_class]"
        payload = self._request_json("GET", endpoint) or {}
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        collected: list[dict[str, str | None]] = []
        for item in jobs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            full_name = str(item.get("fullName") or name).strip()
            if full_name:
                collected.append(
                    {
                        "name": name or full_name,
                        "full_name": full_name,
                        "url": str(item.get("url") or "").strip() or None,
                        "status": str(item.get("color") or "").strip() or None,
                    }
                )
            if self._is_job_container(item):
                collected.extend(self._collect_jobs(full_name))
        return collected

    def _job_path(self, job_name: str) -> str:
        parts = [part.strip() for part in job_name.split("/") if part.strip()]
        return "".join(f"/job/{quote(part)}" for part in parts)

    def _is_job_container(self, payload: dict[str, Any]) -> bool:
        kind = str(payload.get("_class") or "").lower()
        return any(token in kind for token in ("folder", "multibranch", "organizationfolder"))

    def _timestamp_to_iso(self, value: Any) -> str | None:
        try:
            timestamp_ms = int(value)
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()

    def _parse_build_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo_url = ""
        branch = ""
        revision = ""
        actions = payload.get("actions", [])
        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict):
                    continue
                remote_urls = action.get("remoteUrls")
                if isinstance(remote_urls, list):
                    for candidate in remote_urls:
                        if str(candidate).strip():
                            repo_url = str(candidate).strip()
                            break
                revision_payload = action.get("lastBuiltRevision")
                if isinstance(revision_payload, dict):
                    revision = str(revision_payload.get("SHA1") or revision or "").strip()
                    branches = revision_payload.get("branch")
                    if isinstance(branches, list):
                        for item in branches:
                            if not isinstance(item, dict):
                                continue
                            branch = branch or self._normalize_branch_name(str(item.get("name", "")).strip()) or ""
                            revision = revision or str(item.get("SHA1") or "").strip()
                    elif isinstance(branches, dict):
                        branch = branch or self._normalize_branch_name(str(branches.get("name", "")).strip()) or ""
                        revision = revision or str(branches.get("SHA1") or "").strip()
                parameters = action.get("parameters")
                if isinstance(parameters, list):
                    for item in parameters:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", "")).strip().upper()
                        value = str(item.get("value", "")).strip()
                        if not value:
                            continue
                        if not branch and name in {"BRANCH_NAME", "GIT_BRANCH", "BRANCH"}:
                            branch = self._normalize_branch_name(value) or branch
                        if not revision and name in {"GIT_COMMIT", "COMMIT_SHA", "SHA"}:
                            revision = value
                        if not repo_url and name in {"GIT_URL", "REPOSITORY_URL", "REPO_URL"}:
                            repo_url = value
        change_set = payload.get("changeSet")
        if isinstance(change_set, dict) and not revision:
            items = change_set.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    commit_id = str(item.get("commitId", "")).strip()
                    if commit_id:
                        revision = commit_id
                        break
        return {
            "repo_url": repo_url or None,
            "branch": branch or None,
            "revision": revision or None,
            "web_url": payload.get("url"),
            "result": payload.get("result"),
            "id": payload.get("id"),
        }

    def _build_number_from_incident(self, incident: IncidentReport) -> str:
        candidates = [
            str(incident.metadata.get("build_number") or ""),
            str(incident.metadata.get("run_id") or ""),
            incident.id,
            incident.title,
        ]
        for value in candidates:
            if not value:
                continue
            if value.strip().isdigit():
                return value.strip()
            hash_match = re.search(r"#\s*(\d+)", value)
            if hash_match:
                return hash_match.group(1)
            suffix_match = re.search(r"(?:^|[-_])(\d+)$", value)
            if suffix_match:
                return suffix_match.group(1)
        return ""

    def _parse_job_config(self, raw_xml: str) -> dict[str, Any]:
        try:
            root = ElementTree.fromstring(raw_xml)
        except ElementTree.ParseError:
            return {"repo_url": None, "validation_commands": []}

        repo_url = None
        for node in root.findall(".//url"):
            if node.text and node.text.strip():
                repo_url = node.text.strip()
                break

        commands: list[str] = []
        for node in root.findall(".//builders/hudson.tasks.Shell/command"):
            if node.text and node.text.strip():
                commands.extend(self._extract_validation_commands(node.text))
        for node in root.findall(".//builders/hudson.tasks.BatchFile/command"):
            if node.text and node.text.strip():
                commands.extend(self._extract_validation_commands(node.text))
        for node in root.findall(".//definition/script"):
            if node.text and node.text.strip():
                commands.extend(self._extract_pipeline_commands(node.text))

        unique_commands: list[str] = []
        seen: set[str] = set()
        for command in commands:
            normalized = command.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_commands.append(normalized)

        return {
            "repo_url": repo_url,
            "validation_commands": unique_commands,
        }

    def _extract_validation_commands(self, script: str) -> list[str]:
        lines = [line.strip() for line in script.splitlines() if line.strip()]
        test_lines = [line for line in lines if self._looks_like_validation_command(line)]
        return test_lines or lines[-1:]

    def _extract_pipeline_commands(self, script: str) -> list[str]:
        matches = re.findall(r"""(?:sh|bat)\s+['"]([^'"]+)['"]""", script)
        if not matches:
            return []
        selected = [item.strip() for item in matches if self._looks_like_validation_command(item)]
        return selected or [matches[-1].strip()]

    def _looks_like_validation_command(self, command: str) -> bool:
        lowered = command.lower()
        if any(token in lowered for token in ("pip install", "npm install", "pnpm install", "yarn install", "poetry install")):
            return False
        return any(
            token in lowered
            for token in (
                "pytest",
                "unittest",
                "nosetests",
                "tox",
                "go test",
                "gradle test",
                "mvn test",
                "npm test",
                "pnpm test",
                "yarn test",
            )
        )

    def _normalize_branch_name(self, branch: str) -> str | None:
        value = branch.strip()
        if not value:
            return None
        for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/", "remotes/origin/"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
        return value or None

    def _extract_file_references(self, logs: str) -> list[str]:
        files: list[str] = []
        seen: set[str] = set()
        for match in self.FILE_REFERENCE.finditer(logs):
            value = match.group("quoted") or match.group("plain") or ""
            normalized = value.replace("\\", "/").strip().lstrip("./")
            if not normalized or normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            files.append(normalized)
        return files[:20]

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
        last_error: Exception | None = None
        for _ in range(2):
            request = Request(
                self.config.base_url.rstrip("/") + endpoint,
                data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                method=method,
                headers=self._headers(),
            )
            try:
                with urlopen(request, timeout=120) as response:
                    return response.read()
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                if allow_empty_response and exc.code in {200, 201, 202, 204}:
                    return b""
                raise JenkinsCiError(f"Jenkins API returned HTTP {exc.code}: {detail}") from exc
            except (URLError, OSError) as exc:
                last_error = exc
                continue
        raise JenkinsCiError(f"Failed to reach Jenkins API: {last_error}")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.username and self.config.token:
            credentials = f"{self.config.username}:{self.config.token}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("utf-8")
        return headers
