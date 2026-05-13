"""
PR Webhook 接收 + 解析模块（事件采集层）

负责接收来自 GitHub / GitLab 的 PR Webhook 事件，
解析为标准 BugReport，触发后续的 Bug 复盘抽取管道。

支持的事件类型：
- GitHub: pull_request (merged), push, workflow_run (failed)
- GitLab: merge_request (merged), push
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from .models import BugReport, iso_now


class WebhookParseError(Exception):
    """Webhook 解析失败异常"""
    pass


class PRListener:
    """
    PR Webhook 监听器（事件采集层）

    接收 Git 平台的 Webhook 事件，解析为 BugReport 对象。
    - 支持 GitHub pull_request / push / workflow_run 事件
    - 支持 GitLab merge_request / push 事件
    - 支持 Webhook 签名验证（防止伪造）
    """

    def __init__(
        self,
        github_webhook_secret: str | None = None,
        gitlab_webhook_secret: str | None = None,
    ) -> None:
        self.github_webhook_secret = github_webhook_secret
        self.gitlab_webhook_secret = gitlab_webhook_secret

    def verify_github_signature(
        self,
        payload_bytes: bytes,
        signature_header: str | None,
    ) -> bool:
        """验证 GitHub Webhook 签名（HMAC-SHA256）"""
        if not self.github_webhook_secret:
            return True  # 未配置 secret 时跳过验证
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(
            self.github_webhook_secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        received = signature_header.removeprefix("sha256=")
        return hmac.compare_digest(expected, received)

    def verify_gitlab_token(self, token_header: str | None) -> bool:
        """验证 GitLab Webhook Token"""
        if not self.gitlab_webhook_secret:
            return True
        return token_header == self.gitlab_webhook_secret

    def parse_github_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> BugReport | None:
        """
        解析 GitHub Webhook 事件

        Args:
            event_type: X-GitHub-Event 头部值（如 'pull_request'、'push'）
            payload: Webhook 请求体（JSON 解析后）

        Returns:
            BugReport 或 None（不感兴趣的事件）
        """
        if event_type == "pull_request":
            return self._parse_github_pr(payload)
        elif event_type == "push":
            return self._parse_github_push(payload)
        elif event_type == "workflow_run":
            return self._parse_github_workflow_run(payload)
        return None

    def parse_gitlab_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> BugReport | None:
        """
        解析 GitLab Webhook 事件

        Args:
            event_type: X-Gitlab-Event 头部值（如 'Merge Request Hook'）
            payload: Webhook 请求体（JSON 解析后）

        Returns:
            BugReport 或 None（不感兴趣的事件）
        """
        if event_type == "Merge Request Hook":
            return self._parse_gitlab_mr(payload)
        elif event_type in ("Push Hook", "Tag Push Hook"):
            return self._parse_gitlab_push(payload)
        return None

    def parse_manual_input(
        self,
        title: str,
        description: str,
        logs: str = "",
        pr_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> BugReport:
        """
        手动输入 Bug 信息（用于学习/测试场景）
        """
        report_id = self._generate_id(title)
        return BugReport(
            id=report_id,
            title=title,
            description=description,
            logs=logs,
            source_type="manual",
            metadata=dict(metadata or {}),
            reported_at=iso_now(),
        )

    def _parse_github_pr(self, payload: dict[str, Any]) -> BugReport | None:
        """解析 GitHub pull_request 事件"""
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})

        # 只处理合并完成的 PR（bug fix 已经合入）
        if action not in ("closed", "merged") and not payload.get("merged"):
            if action not in ("opened", "reopened", "synchronize"):
                return None

        pr_title = str(pr.get("title", "")).strip()
        pr_body = str(pr.get("body") or "").strip()
        pr_number = pr.get("number", "")
        repo = payload.get("repository", {})
        repo_url = str(repo.get("html_url") or repo.get("clone_url") or "")
        head_sha = str(pr.get("head", {}).get("sha", ""))
        head_branch = str(pr.get("head", {}).get("ref", ""))
        base_branch = str(pr.get("base", {}).get("ref", ""))
        merged = bool(pr.get("merged") or (action == "closed" and pr.get("merged_at")))

        # 判断是否为 bug fix 相关（关键词匹配）
        is_bug_fix = self._is_bug_fix_pr(pr_title, pr_body, head_branch)
        source_type = "pr_merged" if merged else "pr_opened"

        changed_files = [f.get("filename", "") for f in payload.get("files", []) if f.get("filename")]

        return BugReport(
            id=f"gh-pr-{repo.get('full_name', 'unknown').replace('/', '-')}-{pr_number}",
            title=pr_title or f"PR #{pr_number}",
            description=pr_body,
            logs="",
            changed_files=changed_files,
            source_type=source_type,
            pr_id=str(pr_number),
            commit_sha=head_sha,
            repo_url=repo_url,
            metadata={
                "provider": "github",
                "pr_number": pr_number,
                "repo_full_name": repo.get("full_name"),
                "head_branch": head_branch,
                "base_branch": base_branch,
                "merged": merged,
                "is_bug_fix": is_bug_fix,
                "pr_url": str(pr.get("html_url") or ""),
                "author": pr.get("user", {}).get("login", ""),
                "merged_at": pr.get("merged_at"),
                "action": action,
            },
            reported_at=iso_now(),
        )

    def _parse_github_push(self, payload: dict[str, Any]) -> BugReport | None:
        """解析 GitHub push 事件（提取 commit message 中的 bug fix 信息）"""
        commits = payload.get("commits", [])
        if not commits:
            return None

        repo = payload.get("repository", {})
        repo_url = str(repo.get("html_url") or "")
        ref = payload.get("ref", "")
        head_sha = payload.get("after", "")

        # 收集所有 commit message
        all_messages = "\n".join(
            str(c.get("message", "")) for c in commits
        )

        if not self._is_bug_fix_commit(all_messages):
            return None

        changed_files = list(set(
            f for c in commits
            for f in (c.get("added", []) + c.get("modified", []) + c.get("removed", []))
        ))

        branch = ref.removeprefix("refs/heads/")
        title = f"Push to {branch}: {commits[0].get('message', '')[:80]}"

        return BugReport(
            id=f"gh-push-{head_sha[:12]}",
            title=title,
            description=all_messages,
            logs="",
            changed_files=changed_files[:20],
            source_type="push",
            commit_sha=head_sha,
            repo_url=repo_url,
            metadata={
                "provider": "github",
                "branch": branch,
                "commit_count": len(commits),
                "pusher": payload.get("pusher", {}).get("name", ""),
                "repo_full_name": repo.get("full_name"),
            },
            reported_at=iso_now(),
        )

    def _parse_github_workflow_run(self, payload: dict[str, Any]) -> BugReport | None:
        """解析 GitHub workflow_run 事件（CI 失败）"""
        action = payload.get("action", "")
        if action != "completed":
            return None

        run = payload.get("workflow_run", {})
        conclusion = run.get("conclusion", "")
        if conclusion not in ("failure", "cancelled"):
            return None

        repo = payload.get("repository", {})
        repo_url = str(repo.get("html_url") or "")
        run_id = run.get("id", "")
        workflow_name = run.get("name", "Unknown Workflow")
        head_sha = run.get("head_sha", "")
        head_branch = run.get("head_branch", "")

        return BugReport(
            id=f"gh-workflow-{run_id}",
            title=f"CI 失败：{workflow_name} on {head_branch}",
            description=f"GitHub Actions workflow '{workflow_name}' failed with conclusion '{conclusion}'.",
            logs=str(run.get("logs_url") or ""),
            source_type="ci_failure",
            commit_sha=head_sha,
            repo_url=repo_url,
            metadata={
                "provider": "github",
                "workflow_run_id": run_id,
                "workflow_name": workflow_name,
                "conclusion": conclusion,
                "head_branch": head_branch,
                "html_url": run.get("html_url", ""),
                "logs_url": run.get("logs_url", ""),
                "repo_full_name": repo.get("full_name"),
            },
            reported_at=iso_now(),
        )

    def _parse_gitlab_mr(self, payload: dict[str, Any]) -> BugReport | None:
        """解析 GitLab merge_request 事件"""
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action", "")
        state = attrs.get("state", "")

        if action not in ("open", "merge", "close", "update"):
            return None

        mr_title = str(attrs.get("title", "")).strip()
        mr_description = str(attrs.get("description") or "").strip()
        mr_id = attrs.get("iid", "")
        source_branch = attrs.get("source_branch", "")
        target_branch = attrs.get("target_branch", "")
        merge_sha = attrs.get("merge_commit_sha") or attrs.get("last_commit", {}).get("id", "")

        project = payload.get("project", {})
        repo_url = str(project.get("http_url") or project.get("web_url") or "")
        project_id = project.get("id", "")
        merged = state == "merged"
        is_bug_fix = self._is_bug_fix_pr(mr_title, mr_description, source_branch)

        return BugReport(
            id=f"gl-mr-{project_id}-{mr_id}",
            title=mr_title or f"MR !{mr_id}",
            description=mr_description,
            logs="",
            source_type="mr_merged" if merged else "mr_opened",
            pr_id=str(mr_id),
            commit_sha=merge_sha,
            repo_url=repo_url,
            metadata={
                "provider": "gitlab",
                "mr_iid": mr_id,
                "project_id": project_id,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "merged": merged,
                "is_bug_fix": is_bug_fix,
                "url": attrs.get("url", ""),
                "author": payload.get("user", {}).get("username", ""),
                "action": action,
            },
            reported_at=iso_now(),
        )

    def _parse_gitlab_push(self, payload: dict[str, Any]) -> BugReport | None:
        """解析 GitLab push 事件"""
        commits = payload.get("commits", [])
        if not commits:
            return None

        ref = payload.get("ref", "")
        branch = ref.removeprefix("refs/heads/")
        after_sha = payload.get("after", "")
        project = payload.get("project", {})
        repo_url = str(project.get("http_url") or project.get("web_url") or "")

        all_messages = "\n".join(str(c.get("message", "")) for c in commits)
        if not self._is_bug_fix_commit(all_messages):
            return None

        changed_files = list(set(
            f for c in commits
            for f in (c.get("added", []) + c.get("modified", []) + c.get("removed", []))
        ))

        return BugReport(
            id=f"gl-push-{after_sha[:12]}",
            title=f"GitLab Push to {branch}: {commits[0].get('title', '')[:80]}",
            description=all_messages,
            logs="",
            changed_files=changed_files[:20],
            source_type="push",
            commit_sha=after_sha,
            repo_url=repo_url,
            metadata={
                "provider": "gitlab",
                "branch": branch,
                "commit_count": len(commits),
                "project_id": project.get("id"),
            },
            reported_at=iso_now(),
        )

    def _is_bug_fix_pr(self, title: str, body: str, branch: str) -> bool:
        """判断 PR/MR 是否为 Bug 修复相关"""
        text = f"{title} {body} {branch}".lower()
        bug_keywords = [
            "fix", "bug", "bugfix", "hotfix", "patch", "revert",
            "issue", "error", "crash", "fail", "broken",
            "修复", "修改", "解决", "回滚", "问题",
        ]
        return any(kw in text for kw in bug_keywords)

    def _is_bug_fix_commit(self, message: str) -> bool:
        """判断 commit message 是否包含 bug fix 意图"""
        text = message.lower()
        patterns = [
            r"\bfix\b", r"\bbug\b", r"\bhotfix\b", r"\brevert\b",
            r"\bpatch\b", r"fix[: #]",
        ]
        return any(re.search(p, text) for p in patterns)

    def _generate_id(self, text: str) -> str:
        """基于文本生成唯一 ID"""
        slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:30]
        suffix = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        return f"manual-{slug}-{suffix}"
