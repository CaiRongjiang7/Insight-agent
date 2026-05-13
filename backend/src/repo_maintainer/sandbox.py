from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from os import environ
from pathlib import Path

from .config import SandboxConfig
from .models import PatchPlan


@dataclass(slots=True)
class ContextIsolator:
    """
    Context isolator

    在 Bug 复盘场景中，用于隔离 LLM 抽取上下文，防止不同 Bug 报告之间的数据污染。
    维持 Git 沙箱能力，同时扩展上下文隔离语义。
    """
    source_root: Path
    sandbox_path: Path
    baseline_commit: str
    branch_name: str
    limits: SandboxConfig
    runtime_backend: str
    container_runtime_path: str | None = None
    degraded_reason: str = ""

    @classmethod
    def create(
        cls,
        source_root: str | Path,
        sandboxes_dir: str | Path,
        job_id: str,
        sandbox_config: SandboxConfig,
    ) -> "GitSandbox":
        source_path = Path(source_root).resolve()
        sandboxes_root = Path(sandboxes_dir).resolve()
        sandboxes_root.mkdir(parents=True, exist_ok=True)
        sandbox_path = sandboxes_root / job_id
        if sandbox_path.exists():
            shutil.rmtree(sandbox_path)

        ignore = shutil.ignore_patterns("runtime", ".test_runs", "__pycache__", ".pytest_cache", ".mypy_cache")
        shutil.copytree(source_path, sandbox_path, ignore=ignore)

        sandbox = cls(
            source_root=source_path,
            sandbox_path=sandbox_path,
            baseline_commit="",
            branch_name=f"agent/{hashlib.sha1(job_id.encode('utf-8')).hexdigest()[:16]}",
            limits=sandbox_config,
            runtime_backend="workspace",
        )
        if sandbox_config.backend.strip().lower() == "container":
            runtime_path = shutil.which(sandbox_config.container_runtime)
            if runtime_path:
                sandbox.runtime_backend = "container"
                sandbox.container_runtime_path = runtime_path
            else:
                sandbox.degraded_reason = f"{sandbox_config.container_runtime} is not installed; falling back to workspace sandbox."
        sandbox._ensure_git_repo()
        sandbox._configure_identity()
        sandbox._configure_excludes()
        sandbox._run_git(["checkout", "-B", sandbox.branch_name])
        sandbox._run_git(["add", "-A"])
        sandbox._run_git(["commit", "--allow-empty", "-m", "chore: agent sandbox baseline"])
        sandbox.baseline_commit = sandbox._run_git(["rev-parse", "HEAD"]).stdout.strip()
        return sandbox

    def apply_patch_plan(self, plan: PatchPlan) -> None:
        if len(plan.edits) > self.limits.max_patch_edits:
            raise ValueError(
                f"Patch plan exceeds sandbox edit limit: {len(plan.edits)} edits > {self.limits.max_patch_edits}."
            )
        total_edit_chars = sum(len(edit.content) for edit in plan.edits)
        if total_edit_chars > self.limits.max_total_edit_chars:
            raise ValueError(
                "Patch plan exceeds sandbox content limit: "
                f"{total_edit_chars} chars > {self.limits.max_total_edit_chars}."
            )
        delete_count = sum(1 for edit in plan.edits if edit.operation.value == "delete")
        if delete_count > self.limits.max_delete_edits:
            raise ValueError(
                f"Patch plan exceeds delete limit: {delete_count} deletes > {self.limits.max_delete_edits}."
            )
        for edit in plan.edits:
            target = self._resolve_target(edit.path)
            if edit.operation.value == "delete":
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content, encoding="utf-8")

    def rollback(self) -> None:
        self._run_git(["reset", "--hard", self.baseline_commit])
        self._run_git(["clean", "-fd"])

    def commit(self, message: str) -> str | None:
        self._run_git(["add", "-A"])
        status = self._run_git(["status", "--porcelain"]).stdout.strip()
        if not status:
            return None
        self._run_git(["commit", "-m", message])
        return self._run_git(["rev-parse", "HEAD"]).stdout.strip()

    def diff_summary(self) -> str:
        diff = self._run_git(["diff", "--stat", f"{self.baseline_commit}..HEAD"]).stdout.strip()
        return diff or "No diff available."

    def file_text(self, relative_path: str) -> str:
        return self._resolve_target(relative_path).read_text(encoding="utf-8")

    def cleanup(self) -> None:
        shutil.rmtree(self.sandbox_path, ignore_errors=True)

    def run_command(self, command: str, *, timeout_seconds: int, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if self.runtime_backend == "container" and self.container_runtime_path:
            return subprocess.run(
                [
                    self.container_runtime_path,
                    "run",
                    "--rm",
                    "--network",
                    self.limits.container_network_mode,
                    "--cpus",
                    str(self.limits.cpu_limit),
                    "--memory",
                    f"{self.limits.memory_limit_mb}m",
                    "-v",
                    f"{self.sandbox_path}:/workspace",
                    "-w",
                    "/workspace",
                    self.limits.container_image,
                    "sh",
                    "-lc",
                    command,
                ],
                cwd=self.sandbox_path,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env={**environ, **env},
            )
        return subprocess.run(
            command,
            cwd=self.sandbox_path,
            text=True,
            capture_output=True,
            shell=True,
            timeout=timeout_seconds,
            check=False,
            env={**environ, **env},
        )

    def description(self) -> dict[str, str]:
        return {
            "backend": self.runtime_backend,
            "sandbox_path": str(self.sandbox_path),
            "degraded_reason": self.degraded_reason,
        }

    def _ensure_git_repo(self) -> None:
        if (self.sandbox_path / ".git").exists():
            return
        self._run_git(["init"])

    def _configure_identity(self) -> None:
        self._run_git(["config", "user.email", "repo-agent@example.local"])
        self._run_git(["config", "user.name", "Repo Agent"])

    def _configure_excludes(self) -> None:
        exclude_path = self.sandbox_path / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text(
            "\n".join(
                [
                    "__pycache__/",
                    "*.pyc",
                    ".pytest_cache/",
                    ".mypy_cache/",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _resolve_target(self, relative_path: str) -> Path:
        candidate = (self.sandbox_path / relative_path).resolve()
        sandbox_root = self.sandbox_path.resolve()
        if candidate == sandbox_root or sandbox_root not in candidate.parents:
            raise ValueError(f"Unsafe sandbox target path: {relative_path}")
        return candidate

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.sandbox_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Git command failed in sandbox `{self.sandbox_path}`: git {' '.join(args)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        return completed


# 向后兼容别名
GitSandbox = ContextIsolator
