from __future__ import annotations

from contextvars import ContextVar
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

from .flow_debug import flow_log
from .gitlab_ci import GitLabCiClient
from .github_actions import GitHubActionsClient
from .models import ApprovalScope, PreparedIncident
from .security import SecurityManager


_FLOW_INCIDENT_ID: ContextVar[str] = ContextVar("remote_delivery_flow_incident_id", default="")


class WikiPublisher:
    """
    Wiki publisher

    负责将审核通过的 Bug 复盘文档发布到远程平台：
    - GitHub PR / GitLab MR（代码变更交付，现有功能）
    - Markdown Wiki 文件（本地模拟，阶段六实现在线 Wiki）
    """
    def __init__(
        self,
        *,
        github_client: GitHubActionsClient,
        gitlab_client: GitLabCiClient,
        security_manager: SecurityManager,
        github_token: str | None,
        gitlab_token: str | None,
    ) -> None:
        self.github_client = github_client
        self.gitlab_client = gitlab_client
        self.security_manager = security_manager
        self.github_token = github_token
        self.gitlab_token = gitlab_token

    def deliver(
        self,
        *,
        prepared: PreparedIncident,
        sandbox_path: str | Path,
        commit_hash: str,
        diff_summary: str,
        report_path: str,
    ) -> dict[str, str | bool | None]:
        repo_url = str(prepared.incident.metadata.get("repo_url") or "").strip()
        if not repo_url:
            flow_log("remote_delivery.skip", incident_id=prepared.incident.id, reason="incident_missing_repo_url")
            return {"status": "skipped", "reason": "incident_missing_repo_url"}

        flow_log(
            "remote_delivery.start",
            incident_id=prepared.incident.id,
            repo_url=self._sanitize_url(repo_url),
            sandbox_path=sandbox_path,
            commit_hash=commit_hash,
        )
        remote = self._remote_target(prepared, repo_url)
        flow_log("remote_delivery.remote_target", incident_id=prepared.incident.id, remote=remote)
        if remote["provider"] not in {"github", "gitlab"}:
            flow_log(
                "remote_delivery.skip",
                incident_id=prepared.incident.id,
                reason="unsupported_remote_provider",
                repo_url=self._sanitize_url(repo_url),
            )
            return {"status": "skipped", "reason": "unsupported_remote_provider", "repo_url": self._sanitize_url(repo_url)}

        token = self.github_token if remote["provider"] == "github" else self.gitlab_token
        if not token:
            flow_log(
                "remote_delivery.skip",
                incident_id=prepared.incident.id,
                reason=f"missing_{remote['provider']}_token",
                repo_url=self._sanitize_url(repo_url),
            )
            return {"status": "skipped", "reason": f"missing_{remote['provider']}_token", "repo_url": self._sanitize_url(repo_url)}

        branch = self._repair_branch(prepared.incident.id, commit_hash)
        base_branch = self._base_branch(prepared)
        auth_url = self._authenticated_url(repo_url, provider=str(remote["provider"]), token=token)
        flow_log(
            "remote_delivery.branch_plan",
            incident_id=prepared.incident.id,
            provider=remote["provider"],
            branch=branch,
            base_branch=base_branch,
            auth_url=self._sanitize_url(auth_url),
        )
        try:
            write_scope = ApprovalScope.GITHUB_WRITE if remote["provider"] == "github" else ApprovalScope.CI_WRITE
            flow_log(
                "remote_delivery.authorize_write.start",
                incident_id=prepared.incident.id,
                provider=remote["provider"],
                subject=remote["subject"],
                scope=write_scope.value,
            )
            self.security_manager.authorize_remote_write(str(remote["provider"]), str(remote["subject"]), scope=write_scope)
            flow_log("remote_delivery.authorize_write.ok", incident_id=prepared.incident.id, provider=remote["provider"], subject=remote["subject"])
            push_transport = "git"
            try:
                flow_log("remote_delivery.git_push.start", incident_id=prepared.incident.id, sandbox_path=sandbox_path, branch=branch)
                incident_token = _FLOW_INCIDENT_ID.set(prepared.incident.id)
                try:
                    self._push_branch(Path(sandbox_path), auth_url=auth_url, branch=branch)
                finally:
                    _FLOW_INCIDENT_ID.reset(incident_token)
                flow_log("remote_delivery.git_push.ok", incident_id=prepared.incident.id, branch=branch)
            except Exception as push_exc:
                flow_log("remote_delivery.git_push.failed", incident_id=prepared.incident.id, branch=branch, reason=self._sanitize_error(str(push_exc)))
                if remote["provider"] != "github":
                    raise
                flow_log("remote_delivery.github_api_fallback.start", incident_id=prepared.incident.id, branch=branch, base_branch=base_branch)
                self._push_github_via_api(
                    prepared=prepared,
                    sandbox_path=Path(sandbox_path),
                    remote=remote,
                    branch=branch,
                    base_branch=base_branch,
                    message=self._title(prepared),
                    fallback_reason=str(push_exc),
                )
                push_transport = "github_api"
                flow_log("remote_delivery.github_api_fallback.ok", incident_id=prepared.incident.id, branch=branch)
            if remote["provider"] == "github":
                flow_log(
                    "remote_delivery.create_pr.start",
                    incident_id=prepared.incident.id,
                    owner=remote["owner"],
                    repo=remote["repo"],
                    head=branch,
                    base=base_branch,
                )
                pr = self.github_client.create_pull_request(
                    owner=str(remote["owner"]),
                    repo=str(remote["repo"]),
                    title=self._title(prepared),
                    head=branch,
                    base=base_branch,
                    body=self._body(prepared, commit_hash, diff_summary, report_path),
                )
                result = {
                    "status": "created",
                    "provider": "github",
                    "branch": branch,
                    "base_branch": base_branch,
                    "transport": push_transport,
                    "url": str(pr.get("html_url") or ""),
                    "id": str(pr.get("number") or pr.get("id") or ""),
                }
                flow_log("remote_delivery.created", incident_id=prepared.incident.id, result=result)
                return result
            flow_log(
                "remote_delivery.create_mr.start",
                incident_id=prepared.incident.id,
                project_id=remote["project_id"],
                source_branch=branch,
                target_branch=base_branch,
            )
            mr = self.gitlab_client.create_merge_request(
                project_id=str(remote["project_id"]),
                source_branch=branch,
                target_branch=base_branch,
                title=self._title(prepared),
                description=self._body(prepared, commit_hash, diff_summary, report_path),
            )
            result = {
                "status": "created",
                "provider": "gitlab",
                "branch": branch,
                "base_branch": base_branch,
                "url": str(mr.get("web_url") or ""),
                "id": str(mr.get("iid") or mr.get("id") or ""),
            }
            flow_log("remote_delivery.created", incident_id=prepared.incident.id, result=result)
            return result
        except Exception as exc:  # noqa: BLE001 - delivery must not invalidate a verified repair.
            result = {
                "status": "failed",
                "provider": str(remote["provider"]),
                "branch": branch,
                "base_branch": base_branch,
                "reason": self._sanitize_error(str(exc)),
            }
            flow_log("remote_delivery.failed", incident_id=prepared.incident.id, result=result)
            return result

    def _remote_target(self, prepared: PreparedIncident, repo_url: str) -> dict[str, str]:
        metadata = prepared.incident.metadata
        parsed = self._parse_remote_url(repo_url)
        host = parsed.netloc.lower()
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if "github.com" in host:
            owner = str(metadata.get("owner") or "").strip()
            repo = str(metadata.get("repo") or "").strip()
            if not owner or not repo:
                parts = [item for item in path.split("/") if item]
                if len(parts) >= 2:
                    owner, repo = parts[-2], parts[-1]
            return {"provider": "github", "owner": owner, "repo": repo, "subject": f"{owner}/{repo}"}
        if "gitlab" in host:
            project_id = str(metadata.get("project_id") or quote(path, safe="")).strip()
            return {"provider": "gitlab", "project_id": project_id, "subject": project_id}
        return {"provider": "unknown", "subject": self._sanitize_url(repo_url)}

    def _parse_remote_url(self, repo_url: str):
        ssh_match = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", repo_url.strip())
        if ssh_match:
            return urlparse(f"ssh://{ssh_match.group(1)}/{ssh_match.group(2)}")
        return urlparse(repo_url)

    def _push_branch(self, sandbox_path: Path, *, auth_url: str, branch: str) -> None:
        incident_id = _FLOW_INCIDENT_ID.get()
        flow_log("remote_delivery.git_checkout_branch.start", incident_id=incident_id, sandbox_path=sandbox_path, branch=branch)
        checkout = subprocess.run(
            ["git", "checkout", "-B", branch],
            cwd=sandbox_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if checkout.returncode != 0:
            flow_log(
                "remote_delivery.git_checkout_branch.failed",
                incident_id=incident_id,
                returncode=checkout.returncode,
                stdout=self._sanitize_error(checkout.stdout),
                stderr=self._sanitize_error(checkout.stderr),
            )
            raise RuntimeError(
                "git branch creation failed: "
                f"stdout={self._sanitize_error(checkout.stdout)} stderr={self._sanitize_error(checkout.stderr)}"
            )
        flow_log("remote_delivery.git_checkout_branch.ok", incident_id=incident_id, branch=branch)
        flow_log("remote_delivery.git_push_command.start", incident_id=incident_id, branch=branch, auth_url=self._sanitize_url(auth_url))
        completed = subprocess.run(
            ["git", "push", auth_url, f"refs/heads/{branch}:refs/heads/{branch}"],
            cwd=sandbox_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            flow_log(
                "remote_delivery.git_push_command.failed",
                incident_id=incident_id,
                returncode=completed.returncode,
                stdout=self._sanitize_error(completed.stdout),
                stderr=self._sanitize_error(completed.stderr),
            )
            raise RuntimeError(
                "git push failed: "
                f"stdout={self._sanitize_error(completed.stdout)} stderr={self._sanitize_error(completed.stderr)}"
            )
        flow_log("remote_delivery.git_push_command.ok", incident_id=incident_id, branch=branch)

    def _push_github_via_api(
        self,
        *,
        prepared: PreparedIncident,
        sandbox_path: Path,
        remote: dict[str, str],
        branch: str,
        base_branch: str,
        message: str,
        fallback_reason: str,
    ) -> None:
        incident_id = prepared.incident.id
        owner = str(remote["owner"])
        repo = str(remote["repo"])
        flow_log("remote_delivery.github_api.base_sha.start", incident_id=incident_id, owner=owner, repo=repo, base_branch=base_branch)
        base_sha = self._base_sha(prepared, owner=owner, repo=repo, base_branch=base_branch)
        flow_log("remote_delivery.github_api.base_sha.ok", incident_id=incident_id, owner=owner, repo=repo, base_branch=base_branch, base_sha=base_sha)
        self.github_client.create_branch(owner, repo, branch, base_sha)
        flow_log("remote_delivery.github_api.branch.ok", incident_id=incident_id, owner=owner, repo=repo, branch=branch)
        for change in self._changed_files(sandbox_path):
            path = str(change["path"])
            status = str(change["status"])
            flow_log("remote_delivery.github_api.file.start", incident_id=incident_id, path=path, status=status)
            current_sha = self.github_client.get_content_sha(owner, repo, path, branch)
            commit_message = f"{message}\n\nFallback transport: GitHub Contents API.\nOriginal git push failure: {self._sanitize_error(fallback_reason)}"
            if status == "D":
                if current_sha:
                    self.github_client.delete_file(owner, repo, path, branch=branch, message=commit_message, sha=current_sha)
                    flow_log("remote_delivery.github_api.file.deleted", incident_id=incident_id, path=path)
                continue
            content = (sandbox_path / path).read_bytes()
            self.github_client.upsert_file(
                owner,
                repo,
                path,
                branch=branch,
                message=commit_message,
                content=content,
                sha=current_sha,
            )
            flow_log("remote_delivery.github_api.file.upserted", incident_id=incident_id, path=path, has_existing_sha=bool(current_sha))

    def _base_sha(self, prepared: PreparedIncident, *, owner: str, repo: str, base_branch: str) -> str:
        for key in ("head_sha", "sha", "revision", "commit_sha"):
            value = str(prepared.incident.metadata.get(key) or "").strip()
            if re.fullmatch(r"[a-f0-9]{40}", value, re.IGNORECASE):
                return value
        return self.github_client.get_ref_sha(owner, repo, base_branch)

    def _changed_files(self, sandbox_path: Path) -> list[dict[str, str]]:
        completed = subprocess.run(
            ["git", "diff", "--name-status", "HEAD~1..HEAD"],
            cwd=sandbox_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git diff failed: {self._sanitize_error(completed.stderr)}")
        changes: list[dict[str, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0][:1]
            path = parts[-1].replace("\\", "/")
            if path and not path.startswith("../"):
                changes.append({"status": status, "path": path})
        if not changes:
            raise RuntimeError("No changed files were found in the sandbox commit.")
        return changes

    def _authenticated_url(self, repo_url: str, *, provider: str, token: str) -> str:
        parsed = self._parse_remote_url(repo_url)
        if parsed.scheme not in {"http", "https", "ssh"}:
            return repo_url
        path = parsed.path.rstrip("/")
        host = parsed.netloc.removeprefix("git@")
        if provider == "github":
            return f"https://x-access-token:{token}@{host}{path}"
        if provider == "gitlab":
            return f"https://oauth2:{token}@{host}{path}"
        return repo_url

    def _repair_branch(self, incident_id: str, commit_hash: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", incident_id).strip("-").lower() or "incident"
        suffix = re.sub(r"[^A-Za-z0-9]+", "", commit_hash)[:8] or "repair"
        return f"agent/fix-{slug}-{suffix}"

    def _base_branch(self, prepared: PreparedIncident) -> str:
        metadata = prepared.incident.metadata
        for key in ("head_branch", "ref", "branch", "default_branch"):
            value = str(metadata.get(key) or "").strip()
            normalized = self._normalize_branch(value)
            if normalized and not re.fullmatch(r"[a-f0-9]{40}", normalized, re.IGNORECASE):
                return normalized
        return "main"

    def _normalize_branch(self, branch: str) -> str:
        value = branch.strip()
        for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/", "remotes/origin/"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
        return value

    def _title(self, prepared: PreparedIncident) -> str:
        return f"fix: autonomous repair for {prepared.incident.id}"

    def _body(self, prepared: PreparedIncident, commit_hash: str, diff_summary: str, report_path: str) -> str:
        return "\n".join(
            [
                "Autonomous repository repair generated by Repo Autonomy Platform.",
                "",
                f"- Incident: {prepared.incident.title}",
                f"- Incident id: `{prepared.incident.id}`",
                f"- Error type: `{prepared.compressed_error.error_type.value}`",
                f"- Sandbox commit: `{commit_hash}`",
                f"- Local execution report: `{report_path}`",
                "",
                "## Diff Summary",
                "```text",
                diff_summary or "No diff summary available.",
                "```",
                "",
                "Please review the generated changes and let CI validate the repair branch before merging.",
            ]
        )

    def _sanitize_url(self, value: str) -> str:
        return re.sub(r"://([^:@/]+):([^@/]+)@", r"://***:***@", value)

    def _sanitize_error(self, value: str) -> str:
        sanitized = self._sanitize_url(value)
        if self.github_token:
            sanitized = sanitized.replace(self.github_token, "***")
        if self.gitlab_token:
            sanitized = sanitized.replace(self.gitlab_token, "***")
        return sanitized[-2000:]

    def publish_to_local_markdown(
        self,
        document_markdown: str,
        output_path: str,
    ) -> dict[str, str]:
        """
        将 Bug 复盘文档发布到本地 Markdown 文件（Wiki 对接前的预备方案）
        """
        from pathlib import Path
        from .models import iso_now

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document_markdown, encoding="utf-8")
        return {
            "status": "published",
            "target": "local_markdown",
            "path": str(path),
            "published_at": iso_now(),
        }


# 向后兼容别名
RemoteDeliveryManager = WikiPublisher
