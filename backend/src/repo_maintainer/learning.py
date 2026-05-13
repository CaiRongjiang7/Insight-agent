"""
知识演化管道（KnowledgeEvolutionPipeline）

"""
from __future__ import annotations

import re
import threading
from time import monotonic, sleep
from typing import Any

from .memory import MemoryStore
from .models import KnowledgeEntry, Skill, iso_now
from .security import AuditLogger
from .knowledge_base import KnowledgeBase, SkillRepository


def slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return text or "knowledge"


class KnowledgeEvolutionPipeline:
    """Knowledge evolution pipeline"""

    def __init__(self, memory_store: MemoryStore, knowledge_base: KnowledgeBase) -> None:
        self.memory_store = memory_store
        self.knowledge_base = knowledge_base
        self.skill_repository = knowledge_base

    def run(self, limit: int = 20, trace_ids: list[str] | None = None) -> dict[str, object]:
        """运行知识沉淀循环"""
        traces = self._load_traces(limit=limit, trace_ids=trace_ids)
        learned_entry_ids: list[str] = []
        reviewed_trace_ids: list[str] = []

        for trace in traces:
            reviewed_trace_ids.append(trace["id"])
            compressed = trace.get("compressed_error", {})
            repair_summary = str(trace.get("repair_summary", "")).strip()
            if not repair_summary:
                continue
            keywords = list(compressed.get("keywords", []))
            error_type = str(compressed.get("error_type", "unknown"))
            cluster = str(compressed.get("root_cause_cluster", "unknown-cluster"))

            review_doc = trace.get("review_document", {})
            if review_doc and isinstance(review_doc, dict):
                keywords = list(review_doc.get("keywords", keywords))
                repair_summary = review_doc.get("fix_solution", repair_summary) or repair_summary

            similar = self.knowledge_base.find_similar(cluster, error_type, keywords)
            if similar is not None:
                if trace["id"] in similar.source_trace_ids:
                    continue
                similar.version += 1
                similar.triggers = sorted(set(similar.triggers) | set(keywords[:6]))
                similar.keywords = sorted(set(similar.keywords) | set(keywords[:8]))
                similar.description = repair_summary
                similar.action_template = self._derive_action_template(trace)
                similar.source_trace_ids.append(trace["id"])
                similar.last_used_at = iso_now()
                self.knowledge_base.upsert(similar)
                learned_entry_ids.append(similar.id)
                continue

            entry_id = f"ke-learned-{slugify(cluster)}"
            entry = KnowledgeEntry(
                id=entry_id,
                name=f"踩坑：{error_type}",
                description=repair_summary,
                triggers=keywords[:6],
                error_types=[error_type],
                action_template=self._derive_action_template(trace),
                version=1,
                usage_count=0,
                success_count=0,
                last_used_at=iso_now(),
                active=True,
                source_trace_ids=[trace["id"]],
                keywords=keywords[:8],
            )
            self.knowledge_base.upsert(entry)
            learned_entry_ids.append(entry.id)

        retired_entry_ids = self.knowledge_base.govern()
        return {
            "reviewed_traces": len(reviewed_trace_ids),
            "reviewed_trace_ids": reviewed_trace_ids,
            "learned_skill_ids": sorted(set(learned_entry_ids)),
            "learned_entry_ids": sorted(set(learned_entry_ids)),
            "retired_skill_ids": retired_entry_ids,
            "retired_entry_ids": retired_entry_ids,
        }

    def _load_traces(self, limit: int, trace_ids: list[str] | None = None) -> list[dict[str, Any]]:
        if trace_ids:
            ordered_ids = list(dict.fromkeys(trace_ids))
            return self.memory_store.trace_payloads_by_ids(ordered_ids)
        return self.memory_store.successful_traces(limit=limit)

    def _derive_action_template(self, trace: dict[str, object]) -> str:
        executions = list(trace.get("executions", []))
        step_descriptions: list[str] = []
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            for step in execution.get("repair_steps", []):
                if isinstance(step, dict):
                    step_descriptions.append(str(step.get("description", "")))
        if step_descriptions:
            return " ".join(step_descriptions[:3])
        return str(trace.get("repair_summary", "参考历史成功修复模式。"))


# 向后兼容别名
class DreamLearnLoop(KnowledgeEvolutionPipeline):
    """向后兼容别名"""
    def __init__(self, memory_store: MemoryStore, skill_repository: KnowledgeBase) -> None:
        super().__init__(memory_store=memory_store, knowledge_base=skill_repository)


class AutoLearningOrchestrator:
    def __init__(
        self,
        dream_loop: KnowledgeEvolutionPipeline,
        audit_logger: AuditLogger,
        *,
        enabled: bool,
        async_enabled: bool,
        review_limit: int,
        batch_window_seconds: float,
        max_batch_size: int,
        lease_timeout_seconds: float,
        scheduler_lease_ttl_seconds: float,
        idle_poll_seconds: float,
        idle_after_seconds: float,
        idle_backfill_limit: int,
        repair_priority: int,
        idle_backfill_priority: int,
        retry_delay_seconds: float,
        max_retry_delay_seconds: float,
        max_job_failures: int,
        embedded_workers_enabled: bool,
    ) -> None:
        self.dream_loop = dream_loop
        self.memory_store = dream_loop.memory_store
        self.audit_logger = audit_logger
        self.enabled = enabled
        self.async_enabled = async_enabled
        self.review_limit = review_limit
        self.batch_window_seconds = max(batch_window_seconds, 0.0)
        self.max_batch_size = max(max_batch_size, 1)
        self.lease_timeout_seconds = max(lease_timeout_seconds, 1.0)
        self.scheduler_lease_ttl_seconds = max(scheduler_lease_ttl_seconds, 1.0)
        self.idle_poll_seconds = max(idle_poll_seconds, 0.25)
        self.idle_after_seconds = max(idle_after_seconds, 0.0)
        self.idle_backfill_limit = max(idle_backfill_limit, 1)
        self.repair_priority = repair_priority
        self.idle_backfill_priority = idle_backfill_priority
        self.retry_delay_seconds = max(retry_delay_seconds, 1.0)
        self.max_retry_delay_seconds = max(max_retry_delay_seconds, self.retry_delay_seconds)
        self.max_job_failures = max(max_job_failures, 1)
        self.embedded_workers_enabled = embedded_workers_enabled
        self.worker_id = f"learning-worker-{id(self):x}"
        self.scheduler_service_name = "learning-scheduler"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._service_thread: threading.Thread | None = None
        self._last_activity = monotonic()
        self._stats: dict[str, Any] = {
            "mode": "daemon-embedded" if self.embedded_workers_enabled and self.async_enabled else ("daemon-external" if self.async_enabled else "inline"),
            "worker_alive": False,
            "inflight_trace_ids": [],
            "completed_batches": 0,
            "failed_batches": 0,
            "processed_traces": 0,
            "recovered_jobs": 0,
            "last_run_at": None,
            "last_idle_scan_at": None,
            "last_scheduler_action": "",
            "leader": False,
            "leader_owner": "",
            "leader_expires_at": None,
            "last_error": "",
        }
        if self.enabled:
            recovered = self.memory_store.reclaim_stale_learning_jobs(self.lease_timeout_seconds)
            self._update_stats(recovered_jobs=recovered)
        if self.enabled and self.async_enabled and self.embedded_workers_enabled:
            self._service_thread = threading.Thread(target=self.run_forever, name="auto-learning-daemon", daemon=True)
            self._service_thread.start()
            self._update_stats(worker_alive=True)

    def submit(self, trace_id: str, *, priority: int | None = None, source: str = "repair_success") -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "report": {}}
        self.memory_store.enqueue_learning_job(
            trace_id,
            priority=priority if priority is not None else self.repair_priority,
            source=source,
        )
        self.dream_loop.memory_store.update_trace_payload(trace_id, {"auto_learning_status": "queued"})
        self._touch_activity()
        self.audit_logger.record("dream_loop", "queued", trace_id, payload={"source": source})
        if self.async_enabled:
            self._update_stats(last_scheduler_action=f"queued:{source}")
            return {"status": "queued", "report": {}}
        report = self._run_batch([self.memory_store.learning_job(trace_id) or {"trace_id": trace_id}])
        return {"status": "completed", "report": report}

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._stats)
        queue_summary = self.memory_store.learning_job_summary() if self.enabled else {
            "pending": 0,
            "leased": 0,
            "completed": 0,
            "dead_letter": 0,
            "dismissed": 0,
            "active": 0,
            "priority_buckets": {"high": 0, "medium": 0, "low": 0},
        }
        snapshot.update(queue_summary)
        lease = self.memory_store.service_lease(self.scheduler_service_name) if self.enabled else None
        snapshot["enabled"] = self.enabled
        snapshot["async_enabled"] = self.async_enabled
        snapshot["embedded_workers_enabled"] = self.embedded_workers_enabled
        snapshot["queue_size"] = int(queue_summary.get("pending", 0))
        snapshot["unfinished_tasks"] = int(queue_summary.get("active", 0))
        snapshot["worker_alive"] = self._service_thread.is_alive() if self._service_thread is not None else False
        snapshot["leader"] = bool(lease and lease.get("owner_id") == self.worker_id)
        snapshot["leader_owner"] = lease.get("owner_id", "") if lease else ""
        snapshot["leader_expires_at"] = lease.get("lease_expires_at") if lease else None
        return snapshot

    def wait_until_idle(self, timeout_seconds: float = 10.0) -> bool:
        deadline = monotonic() + max(timeout_seconds, 0.1)
        while monotonic() < deadline:
            status = self.status()
            if status["unfinished_tasks"] == 0 and not status.get("inflight_trace_ids"):
                return True
            sleep(0.1)
        return False

    def run_once(self) -> dict[str, Any]:
        if not self.enabled or not self.async_enabled:
            return {"processed_jobs": 0, "leader": False, "idle_enqueued": 0}
        reclaimed = self.memory_store.reclaim_stale_learning_jobs(self.lease_timeout_seconds)
        is_leader = self.memory_store.acquire_service_lease(
            self.scheduler_service_name,
            self.worker_id,
            ttl_seconds=self.scheduler_lease_ttl_seconds,
            payload={"worker_id": self.worker_id, "mode": self._stats.get("mode", "")},
        )
        lease = self.memory_store.service_lease(self.scheduler_service_name)
        self._update_stats(
            recovered_jobs=int(self._stats.get("recovered_jobs", 0)) + reclaimed,
            leader=is_leader,
            leader_owner=lease.get("owner_id", "") if lease else "",
            leader_expires_at=lease.get("lease_expires_at") if lease else None,
        )
        idle_enqueued = self._run_idle_backfill() if is_leader else 0
        claimed_jobs = self.memory_store.claim_learning_jobs(
            self.worker_id,
            limit=self.max_batch_size,
            lease_timeout_seconds=self.lease_timeout_seconds,
        )
        if claimed_jobs and self.batch_window_seconds > 0 and len(claimed_jobs) < self.max_batch_size:
            sleep(self.batch_window_seconds)
            claimed_jobs.extend(
                self.memory_store.claim_learning_jobs(
                    self.worker_id,
                    limit=self.max_batch_size - len(claimed_jobs),
                    lease_timeout_seconds=self.lease_timeout_seconds,
                )
            )
        trace_ids = [str(job.get("trace_id", "")) for job in claimed_jobs if job.get("trace_id")]
        if not trace_ids:
            return {"processed_jobs": 0, "leader": is_leader, "idle_enqueued": idle_enqueued}
        self._touch_activity()
        self._update_stats(inflight_trace_ids=trace_ids)
        try:
            self._run_batch(claimed_jobs)
        finally:
            self._update_stats(inflight_trace_ids=[])
        return {"processed_jobs": len(trace_ids), "leader": is_leader, "idle_enqueued": idle_enqueued}

    def run_forever(self, poll_seconds: float | None = None, max_iterations: int | None = None) -> None:
        self._stop_event.clear()
        sleep_seconds = poll_seconds if poll_seconds is not None else self.idle_poll_seconds
        iterations = 0
        self._update_stats(worker_alive=True)
        try:
            while not self._stop_event.is_set():
                result = self.run_once()
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                if result["processed_jobs"] == 0 and result["idle_enqueued"] == 0:
                    sleep(sleep_seconds)
        finally:
            self._update_stats(worker_alive=False)

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._service_thread is not None:
            self._service_thread.join(timeout=5)
        self._update_stats(worker_alive=False)

    def _run_idle_backfill(self) -> int:
        self._update_stats(last_idle_scan_at=iso_now())
        if monotonic() - self._last_activity < self.idle_after_seconds:
            return 0
        summary = self.memory_store.learning_job_summary()
        if int(summary.get("active", 0)) > 0:
            return 0
        candidates = self.memory_store.unlearned_successful_trace_ids(limit=self.idle_backfill_limit)
        if not candidates:
            return 0
        queued_ids: list[str] = []
        for trace_id in candidates:
            job = self.memory_store.enqueue_learning_job(
                trace_id,
                priority=self.idle_backfill_priority,
                source="idle_backfill",
            )
            if job:
                self.dream_loop.memory_store.update_trace_payload(trace_id, {"auto_learning_status": "queued"})
                queued_ids.append(trace_id)
        if queued_ids:
            self.audit_logger.record(
                "dream_loop",
                "idle_scheduled",
                ",".join(queued_ids),
                payload={"priority": self.idle_backfill_priority, "count": len(queued_ids)},
            )
            self._update_stats(last_scheduler_action=f"idle_backfill:{len(queued_ids)}")
            self._touch_activity()
        return len(queued_ids)

    def _run_batch(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        unique_trace_ids = list(dict.fromkeys(str(job.get("trace_id", "")) for job in jobs if job.get("trace_id")))
        if not unique_trace_ids:
            return {}
        for trace_id in unique_trace_ids:
            self.dream_loop.memory_store.update_trace_payload(trace_id, {"auto_learning_status": "running"})
        self._update_stats(inflight_trace_ids=unique_trace_ids)
        try:
            report = self.dream_loop.run(limit=self.review_limit, trace_ids=unique_trace_ids)
            for trace_id in unique_trace_ids:
                self.memory_store.complete_learning_job(trace_id, report)
                self.dream_loop.memory_store.update_trace_payload(
                    trace_id,
                    {
                        "auto_learning_status": "completed",
                        "auto_learning_report": report,
                    },
                )
            self.audit_logger.record("dream_loop", "completed", ",".join(unique_trace_ids), payload=report)
            with self._lock:
                completed_batches = int(self._stats["completed_batches"]) + 1
                processed_traces = int(self._stats["processed_traces"]) + len(unique_trace_ids)
            self._update_stats(
                completed_batches=completed_batches,
                processed_traces=processed_traces,
                last_run_at=iso_now(),
                last_scheduler_action=f"completed:{len(unique_trace_ids)}",
                last_error="",
            )
            return report
        except Exception as exc:  # noqa: BLE001
            error_payload = {"error": str(exc), "trace_ids": unique_trace_ids}
            for trace_id in unique_trace_ids:
                next_status = self.memory_store.fail_learning_job(
                    trace_id,
                    str(exc),
                    retry_delay_seconds=self.retry_delay_seconds,
                    max_retry_delay_seconds=self.max_retry_delay_seconds,
                    max_failures=self.max_job_failures,
                )
                self.dream_loop.memory_store.update_trace_payload(
                    trace_id,
                    {
                        "auto_learning_status": "queued" if next_status == "pending" else "dead_letter",
                        "auto_learning_report": error_payload,
                    },
                )
            self.audit_logger.record("dream_loop", "failed", ",".join(unique_trace_ids), payload=error_payload)
            with self._lock:
                failed_batches = int(self._stats["failed_batches"]) + 1
                processed_traces = int(self._stats["processed_traces"]) + len(unique_trace_ids)
            self._update_stats(
                failed_batches=failed_batches,
                processed_traces=processed_traces,
                last_run_at=iso_now(),
                last_scheduler_action=f"failed:{len(unique_trace_ids)}",
                last_error=str(exc),
            )
            return error_payload

    def _touch_activity(self) -> None:
        self._last_activity = monotonic()

    def _update_stats(self, **updates: Any) -> None:
        with self._lock:
            self._stats.update(updates)
