from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import ValidationConfig
from .models import BugReport, IncidentReport, ValidationResult
from .security import SecurityManager


class QualityScorer:
    """
    Quality scorer

    
    - 字段完整率：BugReviewDocument 必填字段是否完整
    - 语义一致性：LLM 输出与原始证据是否对齐
    - 历史匹配度：与知识库历史案例的相似程度
    - 来源覆盖率：source_refs 是否充分覆盖核心论断
    """

    def __init__(self, config: ValidationConfig, security_manager: SecurityManager | None = None) -> None:
        self.config = config
        self.security_manager = security_manager

    def score_extraction(
        self,
        document: dict,
        source_text: str = "",
        knowledge_entries: list | None = None,
    ) -> dict[str, float]:
        """
        四维质量评分（用于 Bug 复盘抽取结果评估）

        Returns:
            dict 包含:
            - field_completeness: 字段完整率 [0-1]
            - semantic_consistency: 语义一致性 [0-1]（简单版，待 LLM 增强）
            - history_similarity: 历史匹配度 [0-1]
            - source_coverage: 来源覆盖率 [0-1]
            - overall: 综合得分 [0-1]
        """
        required_fields = ["title", "root_cause", "impact", "fix_solution", "prevention"]
        present = sum(1 for f in required_fields if document.get(f))
        field_completeness = present / len(required_fields)

        source_refs = document.get("source_refs", [])
        root_cause = document.get("root_cause", {})
        snippet = root_cause.get("source", {}).get("snippet", "") if isinstance(root_cause, dict) else ""
        source_coverage = 1.0 if (snippet and snippet in source_text) else (0.5 if snippet else 0.0)

        history_similarity = 0.0
        if knowledge_entries:
            keywords = set(document.get("keywords", []))
            for entry in knowledge_entries:
                entry_kw = set(getattr(entry, "keywords", []) or [])
                if keywords & entry_kw:
                    history_similarity = max(history_similarity, len(keywords & entry_kw) / max(len(keywords | entry_kw), 1))

        semantic_consistency = min(1.0, (field_completeness + source_coverage) / 2.0)

        overall = (field_completeness * 0.35 + semantic_consistency * 0.25 + history_similarity * 0.2 + source_coverage * 0.2)

        return {
            "field_completeness": round(field_completeness, 3),
            "semantic_consistency": round(semantic_consistency, 3),
            "history_similarity": round(history_similarity, 3),
            "source_coverage": round(source_coverage, 3),
            "overall": round(overall, 3),
        }

    def infer_commands(
        self,
        incident: BugReport | IncidentReport,
        repo_root: str | Path,
        llm_commands: list[str] | None = None,
    ) -> list[str]:
        if llm_commands:
            commands = [self._normalize_command(command) for command in llm_commands if command.strip()]
            commands = [command for command in commands if not self._is_bootstrap_command(command)]
            if commands:
                return commands

        metadata = incident.metadata
        commands = metadata.get("validation_commands")
        if isinstance(commands, list) and commands:
            normalized = [self._normalize_command(str(command)) for command in commands if str(command).strip()]
            normalized = [command for command in normalized if not self._is_bootstrap_command(command)]
            if normalized:
                return normalized
        if isinstance(commands, str) and commands.strip():
            normalized = self._normalize_command(commands)
            if not self._is_bootstrap_command(normalized):
                return [normalized]

        for key in ("test_command", "ci_command"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                normalized = self._normalize_command(value)
                if not self._is_bootstrap_command(normalized):
                    return [normalized]

        repo_path = Path(repo_root)
        if (repo_path / "tests").exists():
            return [self._default_test_command()]

        python_files = list(repo_path.rglob("*.py"))
        if python_files:
            return [f'"{sys.executable}" -m compileall .']
        return []

    def _default_test_command(self) -> str:
        if importlib.util.find_spec("pytest") is not None:
            return f'"{sys.executable}" -m pytest -q'
        return f'"{sys.executable}" -m repo_maintainer.simple_test_runner'

    def _normalize_command(self, command: str) -> str:
        normalized = command.strip()
        lowered = normalized.lower()
        normalized = re.sub(
            r"^(python3|python)(\s+-m\s+)",
            lambda match: f'"{sys.executable}"{match.group(2)}',
            normalized,
            flags=re.IGNORECASE,
        )
        lowered = normalized.lower()
        if "pytest" in lowered and importlib.util.find_spec("pytest") is None:
            return f'"{sys.executable}" -m repo_maintainer.simple_test_runner'
        return normalized

    def _is_bootstrap_command(self, command: str) -> bool:
        lowered = command.strip().lower()
        return any(
            token in lowered
            for token in (
                "pip install",
                "npm install",
                "pnpm install",
                "yarn install",
                "poetry install",
            )
        )

    def run(self, repo_root: str | Path, commands: list[str]) -> ValidationResult:
        sandbox_session = None
        if hasattr(repo_root, "run_command"):
            sandbox_session = repo_root
            repo_path = Path(getattr(repo_root, "sandbox_path"))
        else:
            repo_path = Path(repo_root)
        start = time.perf_counter()
        command_results: list[dict[str, object]] = []
        combined_logs: list[str] = []
        success = True
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        support_src = Path(__file__).resolve().parents[1]
        pythonpath_parts = [str(repo_path), str(support_src)]
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

        for command in commands:
            command = command.strip()
            if not command:
                continue
            approvals = []
            if self.security_manager is not None:
                approvals = self.security_manager.authorize_local_command(command)
            try:
                if sandbox_session is not None:
                    completed = sandbox_session.run_command(command, timeout_seconds=self.config.command_timeout_seconds, env=env)
                else:
                    completed = subprocess.run(
                        command,
                        cwd=repo_path,
                        text=True,
                        capture_output=True,
                        shell=True,
                        timeout=self.config.command_timeout_seconds,
                        check=False,
                        env=env,
                    )
                command_success = completed.returncode == 0
                stdout = completed.stdout[-self.config.max_log_chars :]
                stderr = completed.stderr[-self.config.max_log_chars :]
                command_results.append(
                    {
                        "command": command,
                        "returncode": completed.returncode,
                        "success": command_success,
                        "stdout": stdout,
                        "stderr": stderr,
                        "approvals_used": [
                            {
                                "scope": approval.scope.value,
                                "subject": approval.subject,
                                "actor": approval.actor,
                            }
                            for approval in approvals
                        ],
                        "sandbox_backend": getattr(sandbox_session, "runtime_backend", "workspace"),
                    }
                )
                combined_logs.append(f"$ {command}\n{stdout}\n{stderr}".strip())
                if not command_success:
                    success = False
                    break
            except subprocess.TimeoutExpired as exc:
                success = False
                command_results.append(
                    {
                        "command": command,
                        "returncode": -1,
                        "success": False,
                        "stdout": (exc.stdout or "")[-self.config.max_log_chars :],
                        "stderr": f"Timed out after {self.config.command_timeout_seconds} seconds.",
                        "sandbox_backend": getattr(sandbox_session, "runtime_backend", "workspace"),
                    }
                )
                combined_logs.append(f"$ {command}\nTimed out after {self.config.command_timeout_seconds} seconds.")
                break

        duration_seconds = round(time.perf_counter() - start, 3)
        return ValidationResult(
            commands=commands,
            success=success if commands else True,
            command_results=command_results,
            combined_output="\n\n".join(combined_logs)[-self.config.max_log_chars :],
            duration_seconds=duration_seconds,
        )


# 向后兼容别名
class ValidationRunner(QualityScorer):
    """向后兼容别名：ValidationRunner → QualityScorer"""
    pass
