from __future__ import annotations

import os
import json
import shutil
from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .agents import MaintainerAgent
from .agent_tools import AgentToolExecutor
from .config import RuntimeConfig
from .models import AgentExecution, AgentRole, AgentTask


def _execute_task_bundle(payload: dict) -> AgentExecution:
    task = payload["task"]
    incident = payload["incident"]
    compressed_error = payload["compressed_error"]
    context = payload["context"]
    runtime_dir = Path(payload["runtime_dir"])
    runtime_dir.mkdir(parents=True, exist_ok=True)
    sandbox_profile = _prepare_agent_sandbox(
        runtime_dir,
        task,
        context,
        sandbox_mode=str(payload.get("sandbox_mode", "copy")),
        max_files=int(payload.get("sandbox_max_files", 8)),
        network_enabled=bool(payload.get("sandbox_network_enabled", False)),
    )
    context.working_context["active_agent_sandbox"] = deepcopy(sandbox_profile)
    tool_executor = AgentToolExecutor(task, incident, compressed_error, context, sandbox_profile)
    agent = MaintainerAgent(task.role, reasoning_settings=payload.get("agent_reasoning"))
    execution = agent.execute(task, incident, compressed_error, context, tool_executor=tool_executor)
    execution.metadata.update(
        {
            "worker_pid": os.getpid(),
            "runtime_dir": str(runtime_dir),
            "runtime_backend": payload["backend"],
            "tool_permissions": list(task.allowed_tools),
            "sandbox_profile": sandbox_profile,
            "negotiation_round": task.negotiation_round,
        }
    )
    return execution


def _prepare_agent_sandbox(
    runtime_dir: Path,
    task,
    context,
    *,
    sandbox_mode: str,
    max_files: int,
    network_enabled: bool,
) -> dict[str, object]:
    sandbox_root = runtime_dir / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    copied_files: list[str] = []
    repo_root_value = str(context.working_context.get("repo_root", "")).strip()
    if sandbox_mode == "copy" and repo_root_value:
        repo_root = Path(repo_root_value)
        for relative_path in task.focus_files[: max(max_files, 1)]:
            source = (repo_root / relative_path).resolve()
            if not source.exists() or not source.is_file():
                continue
            target = (sandbox_root / relative_path).resolve()
            if sandbox_root.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied_files.append(relative_path)
    manifest = {
        "backend": "agent_workspace",
        "mode": sandbox_mode,
        "root_path": str(sandbox_root),
        "copied_files": copied_files,
        "allowed_tools": list(task.allowed_tools),
        "network_enabled": network_enabled,
        "access": task.sandbox_policy.get("access", "workspace_copy" if sandbox_mode == "copy" else "read_only"),
    }
    (sandbox_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


class ConcurrentAgentRuntime:
    def __init__(self, config: RuntimeConfig, runtime_root: str | Path, agent_reasoning: dict | None = None) -> None:
        self.config = config
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.agent_reasoning = agent_reasoning or {}

    def execute(
        self,
        trace_id: str,
        tasks: list[AgentTask],
        incident,
        compressed_error,
        context,
    ) -> list[AgentExecution]:
        if len(tasks) <= 1:
            return [self._run_single(trace_id, tasks[0], incident, compressed_error, context)]

        if self._supports_negotiation(tasks):
            return self._execute_negotiation(trace_id, tasks, incident, compressed_error, context)

        stage_plan = self._task_stages(tasks)
        if len(stage_plan) == 1:
            if self.config.backend == "thread":
                return self._run_parallel(ThreadPoolExecutor, "thread", trace_id, tasks, incident, compressed_error, context)
            if self.config.backend == "process":
                try:
                    return self._run_parallel(ProcessPoolExecutor, "process", trace_id, tasks, incident, compressed_error, context)
                except Exception:
                    return self._run_parallel(ThreadPoolExecutor, "thread-fallback", trace_id, tasks, incident, compressed_error, context)
            return [self._run_single(trace_id, task, incident, compressed_error, context) for task in tasks]

        executions: dict[str, AgentExecution] = {}
        blackboard: dict[str, object] = {
            "protocol_version": "2.1",
            "notes": [],
            "focus_files": [],
            "risks": [],
            "message_log": [],
            "tool_calls": [],
            "memory_writes": [],
            "debate_log": [],
            "consensus": {},
        }
        for stage_index, stage_tasks in enumerate(stage_plan, start=1):
            stage_context = self._context_with_blackboard(context, blackboard)
            if len(stage_tasks) == 1:
                stage_executions = [self._run_single(trace_id, stage_tasks[0], incident, compressed_error, stage_context)]
            elif self.config.backend == "process":
                try:
                    stage_executions = self._run_parallel(ProcessPoolExecutor, "process", trace_id, stage_tasks, incident, compressed_error, stage_context)
                except Exception:
                    stage_executions = self._run_parallel(ThreadPoolExecutor, "thread-fallback", trace_id, stage_tasks, incident, compressed_error, stage_context)
            elif self.config.backend == "thread":
                stage_executions = self._run_parallel(ThreadPoolExecutor, "thread", trace_id, stage_tasks, incident, compressed_error, stage_context)
            else:
                stage_executions = [self._run_single(trace_id, task, incident, compressed_error, stage_context) for task in stage_tasks]

            for task, execution in zip(stage_tasks, stage_executions):
                execution.metadata.update(
                    {
                        "collaboration_stage": stage_index,
                        "blackboard_reads": len(list(blackboard.get("notes", []))),
                    }
                )
                executions[task.id] = execution
            self._merge_blackboard(blackboard, stage_executions)
        return [executions[task.id] for task in tasks]

    def _execute_negotiation(
        self,
        trace_id: str,
        tasks: list[AgentTask],
        incident,
        compressed_error,
        context,
    ) -> list[AgentExecution]:
        blackboard: dict[str, object] = {
            "protocol_version": "3.0",
            "notes": [],
            "focus_files": [],
            "risks": [],
            "message_log": [],
            "tool_calls": [],
            "memory_writes": [],
            "debate_log": [],
            "consensus": {},
            "debate_rounds": 0,
        }
        executions: list[AgentExecution] = []
        templates: dict[AgentRole, AgentTask] = {task.role: task for task in tasks}
        stage_index = 1

        initial_tasks = [task for task in tasks if task.role in {AgentRole.DIAGNOSE}]
        if initial_tasks:
            stage_executions = self._run_stage(trace_id, initial_tasks, incident, compressed_error, context, blackboard, stage_index)
            self._merge_blackboard(blackboard, stage_executions)
            executions.extend(stage_executions)
            stage_index += 1

        planning_tasks = [task for task in tasks if task.role in {AgentRole.FIX, AgentRole.VALIDATE}]
        if planning_tasks:
            stage_executions = self._run_stage(trace_id, planning_tasks, incident, compressed_error, context, blackboard, stage_index)
            self._merge_blackboard(blackboard, stage_executions)
            executions.extend(stage_executions)
            stage_index += 1

        max_rounds = max(int(self.config.negotiation_max_rounds), 1)
        completed_rounds = 0
        for round_index in range(1, max_rounds + 1):
            critic_template = templates.get(AgentRole.CRITIC)
            rebuttal_template = templates.get(AgentRole.REBUTTAL)
            if critic_template is None or rebuttal_template is None:
                break

            critic_task = replace(
                critic_template,
                id=f"{critic_template.id}-r{round_index}",
                negotiation_round=round_index,
            )
            critic_executions = self._run_stage(trace_id, [critic_task], incident, compressed_error, context, blackboard, stage_index)
            self._merge_blackboard(blackboard, critic_executions)
            executions.extend(critic_executions)
            stage_index += 1

            rebuttal_task = replace(
                rebuttal_template,
                id=f"{rebuttal_template.id}-r{round_index}",
                negotiation_round=round_index,
            )
            rebuttal_executions = self._run_stage(trace_id, [rebuttal_task], incident, compressed_error, context, blackboard, stage_index)
            self._merge_blackboard(blackboard, rebuttal_executions)
            executions.extend(rebuttal_executions)
            stage_index += 1
            completed_rounds = round_index

            blackboard["debate_rounds"] = completed_rounds
            if self._debate_converged(blackboard):
                break

        reflect_template = templates.get(AgentRole.REFLECT)
        if reflect_template is not None:
            reflect_task = replace(
                reflect_template,
                id=f"{reflect_template.id}-final",
                negotiation_round=max(completed_rounds, 1),
            )
            reflect_executions = self._run_stage(trace_id, [reflect_task], incident, compressed_error, context, blackboard, stage_index)
            self._merge_blackboard(blackboard, reflect_executions)
            executions.extend(reflect_executions)
        return executions

    def _run_single(self, trace_id: str, task: AgentTask, incident, compressed_error, context) -> AgentExecution:
        payload = self._payload(trace_id, task, incident, compressed_error, context, "sequential")
        return _execute_task_bundle(payload)

    def _run_parallel(
        self,
        executor_cls,
        backend: str,
        trace_id: str,
        tasks: list[AgentTask],
        incident,
        compressed_error,
        context,
    ) -> list[AgentExecution]:
        executions: dict[str, AgentExecution] = {}
        with executor_cls(max_workers=min(self.config.max_workers, len(tasks))) as executor:
            futures = {
                executor.submit(
                    _execute_task_bundle,
                    self._payload(trace_id, task, incident, compressed_error, context, backend),
                ): task
                for task in tasks
            }
            for future in as_completed(futures, timeout=self.config.task_timeout_seconds):
                task = futures[future]
                execution = future.result(timeout=self.config.task_timeout_seconds)
                executions[task.id] = execution
        return [executions[task.id] for task in tasks]

    def _payload(self, trace_id: str, task: AgentTask, incident, compressed_error, context, backend: str) -> dict:
        runtime_dir = self.runtime_root / trace_id / task.id
        return {
            "trace_id": trace_id,
            "task": task,
            "incident": incident,
            "compressed_error": compressed_error,
            "context": context,
            "runtime_dir": str(runtime_dir),
            "backend": backend,
            "agent_reasoning": self.agent_reasoning,
            "sandbox_mode": self.config.agent_sandbox_mode,
            "sandbox_max_files": self.config.agent_sandbox_max_files,
            "sandbox_network_enabled": self.config.agent_network_enabled,
        }

    def collaboration_snapshot(self, executions: list[AgentExecution]) -> dict[str, object]:
        blackboard: dict[str, object] = {
            "protocol_version": "2.1",
            "notes": [],
            "focus_files": [],
            "risks": [],
            "message_log": [],
            "tool_calls": [],
            "memory_writes": [],
            "debate_log": [],
            "consensus": {},
        }
        self._merge_blackboard(blackboard, executions)
        return blackboard

    def _task_stages(self, tasks: list[AgentTask]) -> list[list[AgentTask]]:
        stage_order = {
            AgentRole.GENERALIST: 1,
            AgentRole.DIAGNOSE: 1,
            AgentRole.FIX: 2,
            AgentRole.VALIDATE: 2,
            AgentRole.CRITIC: 3,
            AgentRole.REBUTTAL: 4,
            AgentRole.REFLECT: 5,
        }
        grouped: dict[int, list[AgentTask]] = {}
        for task in tasks:
            grouped.setdefault(stage_order.get(task.role, 2), []).append(task)
        return [grouped[key] for key in sorted(grouped)]

    def _context_with_blackboard(self, context, blackboard: dict[str, object]):
        working_context = deepcopy(context.working_context)
        working_context["shared_blackboard"] = deepcopy(blackboard)
        return replace(context, working_context=working_context)

    def _run_stage(self, trace_id: str, tasks: list[AgentTask], incident, compressed_error, context, blackboard: dict[str, object], stage_index: int) -> list[AgentExecution]:
        stage_context = self._context_with_blackboard(context, blackboard)
        if len(tasks) == 1:
            stage_executions = [self._run_single(trace_id, tasks[0], incident, compressed_error, stage_context)]
        elif self.config.backend == "process":
            try:
                stage_executions = self._run_parallel(ProcessPoolExecutor, "process", trace_id, tasks, incident, compressed_error, stage_context)
            except Exception:
                stage_executions = self._run_parallel(ThreadPoolExecutor, "thread-fallback", trace_id, tasks, incident, compressed_error, stage_context)
        elif self.config.backend == "thread":
            stage_executions = self._run_parallel(ThreadPoolExecutor, "thread", trace_id, tasks, incident, compressed_error, stage_context)
        else:
            stage_executions = [self._run_single(trace_id, task, incident, compressed_error, stage_context) for task in tasks]
        for task, execution in zip(tasks, stage_executions):
            execution.metadata.update(
                {
                    "collaboration_stage": stage_index,
                    "blackboard_reads": len(list(blackboard.get("notes", []))),
                }
            )
        return stage_executions

    def _supports_negotiation(self, tasks: list[AgentTask]) -> bool:
        roles = {task.role for task in tasks}
        return {AgentRole.FIX, AgentRole.CRITIC, AgentRole.REBUTTAL, AgentRole.REFLECT}.issubset(roles)

    def _debate_converged(self, blackboard: dict[str, object]) -> bool:
        debate_log = [entry for entry in blackboard.get("debate_log", []) if isinstance(entry, dict)]
        challenges = [entry for entry in debate_log if entry.get("stance") == "challenge"]
        rebuttals = [entry for entry in debate_log if entry.get("stance") == "rebut"]
        if not challenges or not rebuttals:
            return False
        last_challenge = challenges[-1]
        last_rebuttal = rebuttals[-1]
        challenge_files = set(last_challenge.get("supporting_files", []))
        rebuttal_files = set(last_rebuttal.get("supporting_files", []))
        file_overlap = self._set_similarity(challenge_files, rebuttal_files)
        claim_overlap = self._set_similarity(
            set(self._tokenize(str(last_challenge.get("claim", "")))),
            set(self._tokenize(str(last_rebuttal.get("claim", "")))),
        )
        note_pressure = min(len(list(blackboard.get("notes", []))), 10) / 10.0
        score = (file_overlap * 0.45) + (claim_overlap * 0.4) + (note_pressure * 0.15)
        blackboard["debate_convergence_score"] = round(score, 6)
        return score >= float(self.config.negotiation_convergence_threshold)

    def _merge_blackboard(self, blackboard: dict[str, object], executions: list[AgentExecution]) -> None:
        notes = list(blackboard.get("notes", []))
        risks = list(blackboard.get("risks", []))
        focus_files = list(blackboard.get("focus_files", []))
        message_log = list(blackboard.get("message_log", []))
        tool_calls = list(blackboard.get("tool_calls", []))
        memory_writes = list(blackboard.get("memory_writes", []))
        debate_log = list(blackboard.get("debate_log", []))
        consensus = dict(blackboard.get("consensus", {})) if isinstance(blackboard.get("consensus", {}), dict) else {}
        for execution in executions:
            target_files = list(
                dict.fromkeys(
                    file_path
                    for step in execution.repair_steps
                    for file_path in step.target_files
                    if file_path
                )
            )[:6]
            notes.append(
                {
                    "task_id": execution.task_id,
                    "role": execution.role.value,
                    "summary": execution.reasoning_summary[:320],
                    "focus_files": target_files,
                    "success_likelihood": round(execution.success_likelihood, 3),
                    "step_titles": [step.title for step in execution.repair_steps[:3]],
                }
            )
            focus_files.extend(file_path for file_path in target_files if file_path not in focus_files)
            if execution.role in {AgentRole.VALIDATE, AgentRole.CRITIC, AgentRole.REBUTTAL, AgentRole.REFLECT} or execution.success_likelihood < 0.65:
                risks.append({"role": execution.role.value, "summary": execution.reasoning_summary[:220]})
            message_log.extend(
                [
                    item
                    for item in execution.metadata.get("collaboration_messages", [])
                    if isinstance(item, dict)
                ]
            )
            tool_calls.extend(
                [
                    item
                    for item in execution.metadata.get("tool_protocol_calls", [])
                    if isinstance(item, dict)
                ]
            )
            memory_write = execution.metadata.get("shared_memory_write")
            if isinstance(memory_write, dict) and memory_write:
                memory_writes.append(memory_write)
            debate = execution.metadata.get("debate_position")
            if isinstance(debate, dict) and debate:
                debate_log.append(debate)
            if execution.role == AgentRole.REFLECT:
                consensus = {
                    "owner": execution.role.value,
                    "summary": execution.reasoning_summary[:320],
                    "step_titles": [step.title for step in execution.repair_steps[:4]],
                    "focus_files": target_files,
                }
        blackboard["notes"] = notes[-12:]
        blackboard["risks"] = risks[-8:]
        blackboard["focus_files"] = focus_files[:12]
        blackboard["message_log"] = message_log[-20:]
        blackboard["tool_calls"] = tool_calls[-20:]
        blackboard["memory_writes"] = memory_writes[-12:]
        blackboard["debate_log"] = debate_log[-12:]
        blackboard["consensus"] = consensus
        blackboard["note_count"] = len(list(blackboard["notes"]))
        blackboard["risk_count"] = len(list(blackboard["risks"]))
        blackboard["message_count"] = len(list(blackboard["message_log"]))
        blackboard["tool_call_count"] = len(list(blackboard["tool_calls"]))
        blackboard["debate_rounds"] = len(
            [
                entry
                for entry in blackboard["debate_log"]
                if isinstance(entry, dict) and entry.get("stance") in {"challenge", "rebut"}
            ]
        )

    def _tokenize(self, text: str) -> list[str]:
        return [token for token in text.lower().replace("\n", " ").split(" ") if token]

    def _set_similarity(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / max(len(left | right), 1)
