from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import SecurityConfig
from .memory import to_jsonable
from .models import ApprovalRecord, ApprovalScope, AuditEvent, iso_now


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SecurityPolicyError(PermissionError):
    pass


class ApprovalStore:
    def __init__(self, approvals_path: str | Path, config: SecurityConfig) -> None:
        self.approvals_path = Path(approvals_path)
        self.config = config
        self.approvals_path.parent.mkdir(parents=True, exist_ok=True)
        self._approvals: list[ApprovalRecord] = []
        self._load()

    def grant(
        self,
        scope: ApprovalScope,
        subject: str,
        actor: str,
        reason: str = "",
        ttl_hours: int | None = None,
    ) -> ApprovalRecord:
        approved_at = _utc_now()
        expires_at = approved_at + timedelta(hours=ttl_hours or self.config.default_approval_ttl_hours)
        record = ApprovalRecord(
            scope=scope,
            subject=subject,
            actor=actor,
            approved_at=approved_at.isoformat(),
            expires_at=expires_at.isoformat(),
            reason=reason,
        )
        self._approvals.append(record)
        self._save()
        return record

    def active_for(self, scope: ApprovalScope, subject: str) -> list[ApprovalRecord]:
        now = _utc_now()
        matched = []
        for approval in self._approvals:
            if approval.scope != scope:
                continue
            if approval.subject not in {subject, "*"}:
                continue
            try:
                expires_at = datetime.fromisoformat(approval.expires_at)
            except ValueError:
                continue
            if expires_at >= now:
                matched.append(approval)
        return matched

    def all(self) -> list[ApprovalRecord]:
        return list(self._approvals)

    def _load(self) -> None:
        if not self.approvals_path.exists():
            return
        try:
            data = json.loads(self.approvals_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = []
        self._approvals = [
            ApprovalRecord(
                scope=ApprovalScope(item["scope"]),
                subject=item["subject"],
                actor=item["actor"],
                approved_at=item["approved_at"],
                expires_at=item["expires_at"],
                reason=item.get("reason", ""),
            )
            for item in data
        ]

    def _save(self) -> None:
        payload = [to_jsonable(asdict(approval)) for approval in self._approvals]
        self.approvals_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class AuditLogger:
    def __init__(self, audit_log_path: str | Path) -> None:
        self.audit_log_path = Path(audit_log_path)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.audit_log_path.exists():
            self.audit_log_path.write_text("", encoding="utf-8")

    def record(self, event_type: str, status: str, subject: str, payload: dict | None = None) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            status=status,
            subject=subject,
            created_at=iso_now(),
            payload=payload or {},
        )
        with self.audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(asdict(event)), ensure_ascii=False) + "\n")
        return event

    def recent(self, limit: int = 100) -> list[dict]:
        lines = self.audit_log_path.read_text(encoding="utf-8").splitlines()
        payloads = [json.loads(line) for line in lines if line.strip()]
        return payloads[-limit:]


class SecurityManager:
    SAFE_LOCAL_COMMAND_PATTERNS = [
        re.compile(r'^(?:"[^"]+"|\S+)\s+-m\s+repo_maintainer\.simple_test_runner(?:\s+.*)?$', re.IGNORECASE),
        re.compile(r'^(?:"[^"]+"|\S+)\s+-m\s+unittest(?:\s+.*)?$', re.IGNORECASE),
        re.compile(r'^(?:"[^"]+"|\S+)\s+-m\s+compileall(?:\s+.*)?$', re.IGNORECASE),
        re.compile(r'^(?:"[^"]+"|\S+)\s+-m\s+pytest(?:\s+.*)?$', re.IGNORECASE),
        re.compile(r'^(?:"[^"]*pytest(?:\.exe)?"|\S*pytest(?:\.exe)?)(?:\s+.*)?$', re.IGNORECASE),
    ]

    def __init__(self, config: SecurityConfig, approval_store: ApprovalStore, audit_logger: AuditLogger) -> None:
        self.config = config
        self.approval_store = approval_store
        self.audit_logger = audit_logger

    def approve(
        self,
        scope: ApprovalScope,
        subject: str,
        actor: str,
        reason: str = "",
        ttl_hours: int | None = None,
    ) -> ApprovalRecord:
        approval = self.approval_store.grant(scope=scope, subject=subject, actor=actor, reason=reason, ttl_hours=ttl_hours)
        self.audit_logger.record(
            event_type="approval_granted",
            status="success",
            subject=f"{scope.value}:{subject}",
            payload={"actor": actor, "reason": reason, "expires_at": approval.expires_at},
        )
        return approval

    def active_approvals(self) -> list[ApprovalRecord]:
        return self.approval_store.all()

    def authorize_local_command(self, command: str) -> list[ApprovalRecord]:
        if any(pattern.match(command.strip()) for pattern in self.SAFE_LOCAL_COMMAND_PATTERNS):
            self.audit_logger.record("local_command", "allowed", command)
            return []
        if self._looks_like_validation_command(command):
            self.audit_logger.record("local_command", "allowed_validation", command)
            return []
        if self.config.allow_unapproved_local_validation:
            self.audit_logger.record("local_command", "allowed_with_warning", command)
            return []
        approvals = self.approval_store.active_for(ApprovalScope.LOCAL_COMMAND, command)
        if approvals:
            self.audit_logger.record("local_command", "approved", command)
            return approvals
        self.audit_logger.record("local_command", "blocked", command)
        raise SecurityPolicyError(
            f"Blocked local validation command `{command}` because it is outside the safe allowlist "
            "and no explicit approval exists."
        )

    def authorize_github_read(self, subject: str) -> list[ApprovalRecord]:
        return self.authorize_remote_read("github", subject)

    def authorize_remote_read(self, channel: str, subject: str) -> list[ApprovalRecord]:
        if not self.config.allow_remote_reads:
            self.audit_logger.record(f"{channel}_read", "blocked", subject)
            raise SecurityPolicyError(f"Remote {channel} reads are disabled by policy.")
        self.audit_logger.record(f"{channel}_read", "allowed", subject)
        return []

    def authorize_github_write(self, subject: str) -> list[ApprovalRecord]:
        return self.authorize_remote_write("github", subject, scope=ApprovalScope.GITHUB_WRITE)

    def authorize_remote_write(
        self,
        channel: str,
        subject: str,
        *,
        scope: ApprovalScope = ApprovalScope.CI_WRITE,
    ) -> list[ApprovalRecord]:
        if not self.config.require_github_write_approval:
            self.audit_logger.record(f"{channel}_write", "allowed", subject)
            return []
        approvals = self.approval_store.active_for(scope, subject)
        if approvals:
            self.audit_logger.record(f"{channel}_write", "approved", subject)
            return approvals
        self.audit_logger.record(f"{channel}_write", "blocked", subject)
        raise SecurityPolicyError(
            f"{channel} write action for `{subject}` requires approval. "
            "Use the approval workflow before rerunning a remote workflow."
        )

    def authorize_remote_execution(self, subject: str) -> list[ApprovalRecord]:
        if subject.startswith("repair:"):
            self.audit_logger.record("remote_execution", "allowed_repair", subject)
            return []
        if not self.config.require_remote_execution_approval:
            self.audit_logger.record("remote_execution", "allowed", subject)
            return []
        approvals = self.approval_store.active_for(ApprovalScope.REMOTE_EXECUTION, subject)
        if approvals:
            self.audit_logger.record("remote_execution", "approved", subject)
            return approvals
        self.audit_logger.record("remote_execution", "blocked", subject)
        raise SecurityPolicyError(
            f"Remote execution for `{subject}` requires approval. "
            "Grant a `remote_execution` approval before running autonomous repairs."
        )

    def summarize_approvals(self, approvals: Iterable[ApprovalRecord]) -> list[dict]:
        return [
            {
                "scope": approval.scope.value,
                "subject": approval.subject,
                "actor": approval.actor,
                "approved_at": approval.approved_at,
                "expires_at": approval.expires_at,
            }
            for approval in approvals
        ]

    def _looks_like_validation_command(self, command: str) -> bool:
        lowered = command.strip().lower()
        if any(token in lowered for token in ("pytest", "unittest", "compileall", "tox", "nosetests")):
            return True
        if any(token in lowered for token in ("npm test", "pnpm test", "yarn test", "go test", "mvn test", "gradle test")):
            return True
        return False
