from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .models import AgentRole, AgentTask, CompressedError, ContextBundle, IncidentReport


class AgentToolError(RuntimeError):
    pass


class AgentToolExecutor:
    SAFE_COMMANDS: tuple[tuple[str, ...], ...] = (
        ("python", "-m", "compileall"),
        ("python", "-m", "pytest"),
        ("pytest",),
        ("git", "diff"),
        ("git", "status"),
    )

    def __init__(
        self,
        task: AgentTask,
        incident: IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
        sandbox_profile: dict[str, Any],
    ) -> None:
        self.task = task
        self.incident = incident
        self.compressed_error = compressed_error
        self.context = context
        self.sandbox_profile = sandbox_profile
        self.allowed_tools = set(task.allowed_tools)
        self.sandbox_root = Path(str(sandbox_profile.get("root_path", "") or "")).resolve() if sandbox_profile.get("root_path") else None
        repo_root = str(context.working_context.get("repo_root", "") or "").strip()
        self.repo_root = Path(repo_root).resolve() if repo_root else None

    def run_default_plan(self) -> list[dict[str, Any]]:
        tool_calls = self._default_plan()
        results: list[dict[str, Any]] = []
        for tool_name, args in tool_calls:
            if tool_name not in self.allowed_tools:
                continue
            results.append(self.execute(tool_name, args))
        return results

    def execute(self, tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool_name not in self.allowed_tools:
            raise AgentToolError(f"Tool `{tool_name}` is not allowed for task `{self.task.id}`.")
        args = dict(args or {})
        started = time.perf_counter()
        try:
            output = self._dispatch(tool_name, args)
            status = "completed"
        except Exception as exc:  # noqa: BLE001
            output = {"error": str(exc)}
            status = "failed"
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "actor": self.task.role.value,
            "tool_name": tool_name,
            "arguments": args,
            "output": output,
            "status": status,
            "duration_ms": duration_ms,
        }

    def _default_plan(self) -> list[tuple[str, dict[str, Any]]]:
        query = " ".join(
            [
                self.incident.title,
                self.compressed_error.semantic_summary,
                self.compressed_error.root_cause_cluster,
            ]
        ).strip()
        focus_files = self.task.focus_files[:6] or self.context.relevant_files[:6]
        role_plan: dict[AgentRole, list[tuple[str, dict[str, Any]]]] = {
            AgentRole.DIAGNOSE: [
                ("context.inspect", {"files": focus_files}),
                ("graph.trace", {"files": focus_files}),
                ("memory.lookup", {"query": query, "limit": 4}),
                ("log.inspect", {"limit": 20}),
            ],
            AgentRole.FIX: [
                ("context.inspect", {"files": focus_files[:4]}),
                ("patch.scope", {"files": focus_files}),
                ("memory.lookup", {"query": query, "limit": 3}),
                ("sandbox.exec", {"command": [sys.executable, "-m", "compileall", "."]}),
            ],
            AgentRole.VALIDATE: [
                ("validation.plan", {"files": focus_files}),
                ("log.inspect", {"limit": 12}),
                ("sandbox.exec", {"command": [sys.executable, "-m", "compileall", "."]}),
                ("risk.rank", {"limit": 5}),
            ],
            AgentRole.CRITIC: [
                ("graph.trace", {"files": focus_files}),
                ("debate.challenge", {"files": focus_files}),
                ("risk.rank", {"limit": 5}),
                ("memory.lookup", {"query": query, "limit": 2}),
            ],
            AgentRole.REBUTTAL: [
                ("debate.rebuttal", {"files": focus_files}),
                ("patch.scope", {"files": focus_files}),
                ("validation.plan", {"files": focus_files}),
                ("sandbox.exec", {"command": [sys.executable, "-m", "compileall", "."]}),
            ],
            AgentRole.REFLECT: [
                ("consensus.merge", {"files": focus_files}),
                ("risk.rank", {"limit": 6}),
                ("memory.write", {"cluster": self.compressed_error.root_cause_cluster}),
            ],
            AgentRole.GENERALIST: [
                ("context.inspect", {"files": focus_files}),
                ("graph.trace", {"files": focus_files}),
                ("memory.lookup", {"query": query, "limit": 4}),
                ("patch.scope", {"files": focus_files}),
                ("validation.plan", {"files": focus_files}),
                ("sandbox.exec", {"command": [sys.executable, "-m", "compileall", "."]}),
            ],
        }
        return role_plan.get(self.task.role, [])

    def _dispatch(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "context.inspect": self._context_inspect,
            "graph.trace": self._graph_trace,
            "memory.lookup": self._memory_lookup,
            "patch.scope": self._patch_scope,
            "validation.plan": self._validation_plan,
            "log.inspect": self._log_inspect,
            "debate.challenge": self._debate_challenge,
            "debate.rebuttal": self._debate_rebuttal,
            "risk.rank": self._risk_rank,
            "consensus.merge": self._consensus_merge,
            "memory.write": self._memory_write,
            "orchestrator.plan": self._orchestrator_plan,
            "sandbox.exec": self._sandbox_exec,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            raise AgentToolError(f"Unsupported tool `{tool_name}`.")
        return handler(args)

    def _context_inspect(self, args: dict[str, Any]) -> dict[str, Any]:
        files = self._normalize_files(args.get("files"))
        inspected: list[dict[str, Any]] = []
        for file_path in files[:6]:
            resolved = self._resolve_file(file_path)
            if resolved is None or not resolved.exists() or not resolved.is_file():
                inspected.append({"path": file_path, "exists": False})
                continue
            try:
                text = resolved.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = resolved.read_text(encoding="utf-8", errors="ignore")
            inspected.append(
                {
                    "path": file_path,
                    "exists": True,
                    "size": len(text),
                    "preview": text[:600],
                }
            )
        return {"files": inspected}

    def _graph_trace(self, args: dict[str, Any]) -> dict[str, Any]:
        files = set(self._normalize_files(args.get("files")))
        root_paths = self.context.working_context.get("root_cause_paths", [])
        data_flow = self.context.working_context.get("data_flow_slice", [])
        control_flow = self.context.working_context.get("control_flow_slice", [])
        return {
            "root_cause_paths": [
                item
                for item in root_paths[:6]
                if not files or any(str(segment) in files for segment in item.get("path", []))
            ]
            or root_paths[:3],
            "data_flow_slice": self._filter_edges(data_flow, files)[:12],
            "control_flow_slice": self._filter_edges(control_flow, files)[:12],
            "neighbor_files": list(self.context.working_context.get("neighbor_files", []))[:8],
        }

    def _memory_lookup(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).lower()
        limit = int(args.get("limit", 4))
        query_terms = set(token for token in query.replace("\n", " ").split(" ") if token)
        candidates = []
        for memory in self.context.long_term_memories + self.context.short_term_memories:
            haystack = json.dumps(memory, ensure_ascii=False).lower()
            overlap = len(query_terms & set(haystack.split(" ")))
            candidates.append((overlap, memory))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return {"matches": [item for _, item in candidates[:limit] if item]}

    def _patch_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        files = self._normalize_files(args.get("files"))
        scope: list[dict[str, Any]] = []
        for file_path in files[:6]:
            resolved = self._resolve_file(file_path)
            if resolved is None or not resolved.exists() or not resolved.is_file():
                continue
            try:
                text = resolved.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = resolved.read_text(encoding="utf-8", errors="ignore")
            scope.append(
                {
                    "path": file_path,
                    "line_count": text.count("\n") + 1,
                    "writable": self.sandbox_profile.get("access") != "read_only",
                    "preview": text[:500],
                }
            )
        return {"scope": scope}

    def _validation_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        files = self._normalize_files(args.get("files"))
        commands: list[list[str]] = []
        python_focus = any(file_path.endswith(".py") for file_path in files)
        if python_focus:
            commands.append([sys.executable, "-m", "compileall", "."])
        if any("test" in Path(file_path).name.lower() for file_path in files):
            commands.append([sys.executable, "-m", "pytest", "-q"])
        return {"commands": commands or [[sys.executable, "-m", "compileall", "."]], "focus_files": files[:6]}

    def _log_inspect(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 16))
        lines = [
            line.strip()
            for line in self.incident.logs.splitlines()
            if line.strip() and any(token in line.lower() for token in ("error", "fail", "traceback", "exception"))
        ]
        return {"matches": lines[:limit], "cluster": self.compressed_error.root_cause_cluster}

    def _debate_challenge(self, args: dict[str, Any]) -> dict[str, Any]:
        blackboard = self._blackboard()
        fix_notes = [note for note in blackboard.get("notes", []) if isinstance(note, dict) and note.get("role") == "fix"]
        claim = fix_notes[-1].get("summary", "No fix proposal found.") if fix_notes else "No fix proposal found."
        return {
            "claim": claim,
            "objections": [
                "Patch scope may be wider than the proven root-cause path.",
                "Validation evidence is weaker than the repair confidence.",
            ],
            "supporting_files": list(blackboard.get("focus_files", []))[:6],
        }

    def _debate_rebuttal(self, args: dict[str, Any]) -> dict[str, Any]:
        blackboard = self._blackboard()
        critic_notes = [note for note in blackboard.get("notes", []) if isinstance(note, dict) and note.get("role") == "critic"]
        challenge = critic_notes[-1].get("summary", "No critic objection found.") if critic_notes else "No critic objection found."
        return {
            "challenge": challenge,
            "response": "Constrain edits to the files that appear in both the fix scope and root-cause path, then validate syntax before broader tests.",
            "supporting_files": list(blackboard.get("focus_files", []))[:6] or self.task.focus_files[:6],
        }

    def _risk_rank(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 5))
        blackboard = self._blackboard()
        risks = []
        for note in blackboard.get("risks", []):
            if not isinstance(note, dict):
                continue
            risks.append(
                {
                    "role": note.get("role"),
                    "summary": note.get("summary"),
                    "score": round(0.55 + (0.05 * len(str(note.get("summary", "")).split(" "))), 3),
                }
            )
        risks.sort(key=lambda item: item["score"], reverse=True)
        return {"risks": risks[:limit]}

    def _consensus_merge(self, args: dict[str, Any]) -> dict[str, Any]:
        blackboard = self._blackboard()
        notes = [note for note in blackboard.get("notes", []) if isinstance(note, dict)]
        focus_files = list(blackboard.get("focus_files", []))[:6] or self.task.focus_files[:6]
        return {
            "summary": " | ".join(str(note.get("summary", ""))[:120] for note in notes[:4]),
            "focus_files": focus_files,
            "step_titles": [title for note in notes for title in note.get("step_titles", [])][:6],
        }

    def _memory_write(self, args: dict[str, Any]) -> dict[str, Any]:
        cluster = str(args.get("cluster", self.compressed_error.root_cause_cluster))
        return {
            "cluster": cluster,
            "role": self.task.role.value,
            "summary": self.compressed_error.semantic_summary[:180],
            "focus_files": self.task.focus_files[:6],
        }

    def _orchestrator_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "incident": self.incident.id,
            "error_type": self.compressed_error.error_type.value,
            "focus_files": self.task.focus_files[:6],
            "available_tools": sorted(self.allowed_tools),
        }

    def _sandbox_exec(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.sandbox_root is None or not self.sandbox_root.exists():
            raise AgentToolError("Sandbox is not available for command execution.")
        if self.sandbox_profile.get("access") == "read_only":
            raise AgentToolError("Read-only sandbox cannot execute mutable commands.")
        raw_command = args.get("command", [])
        if isinstance(raw_command, str):
            command = raw_command.split()
        else:
            command = [str(part) for part in raw_command]
        command = self._normalize_command(command)
        if not command:
            raise AgentToolError("Empty sandbox command.")
        if not self._command_allowed(command):
            raise AgentToolError(f"Sandbox command is not allowed: {' '.join(command)}")
        completed = subprocess.run(
            command,
            cwd=self.sandbox_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-3000:],
            "stderr": completed.stderr[-3000:],
        }

    def _normalize_files(self, raw_files: Any) -> list[str]:
        if not isinstance(raw_files, list):
            return list(self.task.focus_files[:6])
        return [str(file_path).replace("\\", "/") for file_path in raw_files if str(file_path).strip()]

    def _resolve_file(self, relative_path: str) -> Path | None:
        clean_path = Path(relative_path)
        for root in [self.sandbox_root, self.repo_root]:
            if root is None:
                continue
            candidate = (root / clean_path).resolve()
            if candidate.exists() and root in candidate.parents:
                return candidate
        return None

    def _filter_edges(self, edges: Any, files: set[str]) -> list[dict[str, Any]]:
        if not isinstance(edges, list):
            return []
        filtered: list[dict[str, Any]] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source_file", edge.get("source", "")))
            target = str(edge.get("target_file", edge.get("target", "")))
            if files and source not in files and target not in files:
                continue
            filtered.append(edge)
        return filtered

    def _blackboard(self) -> dict[str, Any]:
        blackboard = self.context.working_context.get("shared_blackboard", {})
        return blackboard if isinstance(blackboard, dict) else {}

    def _normalize_command(self, command: list[str]) -> list[str]:
        if not command:
            return []
        normalized = list(command)
        executable = Path(normalized[0]).name.lower()
        if executable.startswith("python"):
            normalized[0] = "python"
        return normalized

    def _command_allowed(self, command: list[str]) -> bool:
        lowered = [part.lower() for part in command]
        for prefix in self.SAFE_COMMANDS:
            if tuple(lowered[: len(prefix)]) == prefix:
                return True
        return False
