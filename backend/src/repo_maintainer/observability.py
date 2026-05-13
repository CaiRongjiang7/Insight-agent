from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from .memory import to_jsonable
from .models import ExecutionTrace


class MetricsCollector:
    def __init__(self, metrics_path: str | Path) -> None:
        self.metrics_path = Path(metrics_path)
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.metrics_path.exists():
            self.metrics_path.write_text("", encoding="utf-8")

    def record(self, trace: ExecutionTrace) -> None:
        retrieval_scores = [float(match.get("score", 0.0)) for match in trace.retrieval_matches if isinstance(match, dict)]
        record = {
            "trace_id": trace.id,
            "incident_type": trace.compressed_error.error_type.value,
            "skill_hits": len(trace.matched_skill_ids),
            "retrieval_hits": len(trace.retrieval_matches),
            "avg_retrieval_score": round(mean(retrieval_scores), 6) if retrieval_scores else 0.0,
            "success": trace.success,
            "token_usage": trace.token_usage,
            "task_chain_length": len(trace.executions),
            "relevant_file_count": len(trace.context_digest.get("relevant_files", [])),
            "root_cause_path_count": len(trace.context_digest.get("root_cause_paths", [])),
            "llm_reasoned_tasks": sum(1 for execution in trace.executions if execution.metadata.get("reasoning_source") == "llm"),
            "critic_tasks": sum(1 for execution in trace.executions if execution.role.value == "critic"),
            "reflective_tasks": sum(1 for execution in trace.executions if execution.role.value == "reflect"),
            "blackboard_notes": len(trace.collaboration_blackboard.get("notes", [])),
            "message_count": len(trace.collaboration_blackboard.get("message_log", [])),
            "tool_call_count": len(trace.collaboration_blackboard.get("tool_calls", [])),
            "memory_write_count": len(trace.collaboration_blackboard.get("memory_writes", [])),
            "mode": trace.decision.mode,
            "execution_mode": trace.execution_mode,
            "validation_passed": trace.validation_passed,
            "attempt_count": trace.attempt_count,
            "rollback_performed": trace.rollback_performed,
            "dream_run_triggered": trace.auto_learning_status != "not_requested",
            "dream_completed": trace.auto_learning_status == "completed",
            "auto_learning_status": trace.auto_learning_status,
            "dream_learned_skills": len(trace.auto_learning_report.get("learned_skill_ids", [])),
            "github_run_id": trace.github_run_id,
            "approvals_used": len(trace.approvals_used),
            "runtime_backend": trace.agent_runtime_backend,
            "created_at": trace.created_at,
        }
        with self.metrics_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")

    def summary(self, limit: int = 100) -> dict[str, Any]:
        records = self._load_records(limit)
        if not records:
            return {
                "runs": 0,
                "skill_hit_rate": 0.0,
                "repair_success_rate": 0.0,
                "avg_token_usage": 0.0,
                "avg_task_chain_length": 0.0,
                "multi_agent_rate": 0.0,
                "validation_pass_rate": 0.0,
                "rollback_rate": 0.0,
                "github_incident_rate": 0.0,
                "avg_retrieval_score": 0.0,
                "avg_relevant_file_count": 0.0,
                "avg_root_cause_path_count": 0.0,
                "agent_llm_reasoning_rate": 0.0,
                "critic_rate": 0.0,
                "reflection_rate": 0.0,
                "avg_message_count": 0.0,
                "avg_tool_call_count": 0.0,
                "auto_dream_rate": 0.0,
                "auto_dream_completion_rate": 0.0,
                "avg_dream_learned_skills": 0.0,
            }

        return {
            "runs": len(records),
            "skill_hit_rate": round(sum(1 for record in records if record["skill_hits"] > 0) / len(records), 3),
            "repair_success_rate": round(sum(1 for record in records if record["success"]) / len(records), 3),
            "avg_token_usage": round(mean(record["token_usage"] for record in records), 2),
            "avg_task_chain_length": round(mean(record["task_chain_length"] for record in records), 2),
            "multi_agent_rate": round(sum(1 for record in records if record["mode"] == "multi_agent") / len(records), 3),
            "validation_pass_rate": round(
                sum(1 for record in records if record.get("validation_passed") is True) / max(len(records), 1),
                3,
            ),
            "rollback_rate": round(
                sum(1 for record in records if record.get("rollback_performed")) / max(len(records), 1),
                3,
            ),
            "github_incident_rate": round(
                sum(1 for record in records if record.get("github_run_id") is not None) / max(len(records), 1),
                3,
            ),
            "avg_retrieval_score": round(mean(float(record.get("avg_retrieval_score", 0.0)) for record in records), 6),
            "avg_relevant_file_count": round(mean(int(record.get("relevant_file_count", 0)) for record in records), 2),
            "avg_root_cause_path_count": round(mean(int(record.get("root_cause_path_count", 0)) for record in records), 2),
            "agent_llm_reasoning_rate": round(
                sum(1 for record in records if int(record.get("llm_reasoned_tasks", 0)) > 0) / len(records),
                3,
            ),
            "critic_rate": round(
                sum(1 for record in records if int(record.get("critic_tasks", 0)) > 0) / len(records),
                3,
            ),
            "reflection_rate": round(
                sum(1 for record in records if int(record.get("reflective_tasks", 0)) > 0) / len(records),
                3,
            ),
            "avg_message_count": round(mean(int(record.get("message_count", 0)) for record in records), 2),
            "avg_tool_call_count": round(mean(int(record.get("tool_call_count", 0)) for record in records), 2),
            "auto_dream_rate": round(
                sum(1 for record in records if bool(record.get("dream_run_triggered"))) / len(records),
                3,
            ),
            "auto_dream_completion_rate": round(
                sum(1 for record in records if bool(record.get("dream_completed"))) / len(records),
                3,
            ),
            "avg_dream_learned_skills": round(
                mean(int(record.get("dream_learned_skills", 0)) for record in records),
                2,
            ),
        }

    def records(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._load_records(limit)

    def _load_records(self, limit: int) -> list[dict[str, Any]]:
        lines = self.metrics_path.read_text(encoding="utf-8").splitlines()
        payloads = [json.loads(line) for line in lines if line.strip()]
        return payloads[-limit:]
