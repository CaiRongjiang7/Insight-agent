from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import ExecutionTrace


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


class MemoryStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    incident_type TEXT NOT NULL,
                    cluster TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    repair_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_jobs (
                    trace_id TEXT PRIMARY KEY,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    leased_by TEXT,
                    leased_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'repair_success',
                    payload TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_learning_jobs_status_priority
                ON learning_jobs(status, priority DESC, available_at, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_leases (
                    service_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS collaboration_memories (
                    memory_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    incident_type TEXT NOT NULL,
                    cluster TEXT NOT NULL,
                    role TEXT NOT NULL,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_collaboration_memories_lookup
                ON collaboration_memories(incident_type, cluster, created_at)
                """
            )
            connection.commit()

    def append_trace(self, trace: ExecutionTrace) -> None:
        payload = json.dumps(to_jsonable(trace), ensure_ascii=False)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO traces (
                    trace_id, incident_type, cluster, success, repair_summary, created_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.id,
                    trace.compressed_error.error_type.value,
                    trace.compressed_error.root_cause_cluster,
                    int(trace.success),
                    trace.repair_summary,
                    trace.created_at,
                    payload,
                ),
            )
            connection.commit()

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT trace_id, incident_type, cluster, success, repair_summary, created_at
                FROM traces
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "trace_id": row[0],
                "incident_type": row[1],
                "cluster": row[2],
                "success": bool(row[3]),
                "repair_summary": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

    def related(
        self,
        incident_type: str,
        cluster: str,
        keywords: list[str],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        like_terms = [keyword.lower() for keyword in keywords[:3]]
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT trace_id, incident_type, cluster, success, repair_summary, created_at, payload
                FROM traces
                WHERE incident_type = ? OR cluster = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (incident_type, cluster, limit * 3),
            ).fetchall()
        related_rows = []
        for row in rows:
            payload = row[6].lower()
            if cluster == row[2] or any(keyword in payload for keyword in like_terms):
                related_rows.append(
                    {
                        "trace_id": row[0],
                        "incident_type": row[1],
                        "cluster": row[2],
                        "success": bool(row[3]),
                        "repair_summary": row[4],
                        "created_at": row[5],
                    }
                )
            if len(related_rows) >= limit:
                break
        return related_rows

    def successful_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM traces
                WHERE success = 1
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT trace_id, incident_type, cluster, success, repair_summary, created_at
                FROM traces
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "trace_id": row[0],
                "incident_type": row[1],
                "cluster": row[2],
                "success": bool(row[3]),
                "repair_summary": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

    def trace_payloads(self, limit: int = 50) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM traces
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def trace_payload(self, trace_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT payload
                FROM traces
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def enqueue_learning_job(
        self,
        trace_id: str,
        *,
        priority: int,
        source: str,
        available_at: str | None = None,
        force: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        ready_at = available_at or now
        payload_blob = json.dumps(to_jsonable(payload or {}), ensure_ascii=False)
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT priority, status, available_at, attempts, source, payload
                FROM learning_jobs
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO learning_jobs (
                        trace_id, priority, status, available_at, created_at, updated_at,
                        leased_by, leased_at, attempts, last_error, source, payload
                    ) VALUES (?, ?, 'pending', ?, ?, ?, NULL, NULL, 0, '', ?, ?)
                    """,
                    (trace_id, priority, ready_at, now, now, source, payload_blob),
                )
                connection.commit()
                return self.learning_job(trace_id) or {}

            if row[1] == "completed" and not force:
                return self.learning_job(trace_id) or {}

            if row[1] in {"pending", "leased"} and not force:
                connection.execute(
                    """
                    UPDATE learning_jobs
                    SET priority = ?, updated_at = ?, source = ?, payload = ?
                    WHERE trace_id = ?
                    """,
                    (max(int(row[0]), priority), now, source, payload_blob, trace_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE learning_jobs
                    SET priority = ?, status = 'pending', available_at = ?, updated_at = ?,
                        leased_by = NULL, leased_at = NULL, last_error = '', attempts = 0, source = ?, payload = ?
                    WHERE trace_id = ?
                    """,
                    (priority, ready_at, now, source, payload_blob, trace_id),
                )
            connection.commit()
        return self.learning_job(trace_id) or {}

    def learning_job(self, trace_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM learning_jobs WHERE trace_id = ?", (trace_id,)).fetchone()
        if row is None:
            return None
        return self._learning_job_from_row(row)

    def claim_learning_jobs(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        now = self._now()
        stale_before = self._shifted_time(-lease_timeout_seconds)
        claimed_trace_ids: list[str] = []
        with sqlite3.connect(self.database_path) as connection:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT trace_id
                FROM learning_jobs
                WHERE available_at <= ?
                  AND (
                    status = 'pending'
                    OR (status = 'leased' AND (leased_at IS NULL OR leased_at <= ?))
                  )
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (now, stale_before, limit),
            ).fetchall()
            for row in rows:
                trace_id = row[0]
                updated = connection.execute(
                    """
                    UPDATE learning_jobs
                    SET status = 'leased', leased_by = ?, leased_at = ?, updated_at = ?,
                        attempts = attempts + 1
                    WHERE trace_id = ?
                      AND available_at <= ?
                      AND (
                        status = 'pending'
                        OR (status = 'leased' AND (leased_at IS NULL OR leased_at <= ?))
                      )
                    """,
                    (worker_id, now, now, trace_id, now, stale_before),
                )
                if updated.rowcount:
                    claimed_trace_ids.append(trace_id)
            connection.commit()
        return [self.learning_job(trace_id) or {} for trace_id in claimed_trace_ids]

    def reclaim_stale_learning_jobs(self, lease_timeout_seconds: float) -> int:
        stale_before = self._shifted_time(-lease_timeout_seconds)
        now = self._now()
        with sqlite3.connect(self.database_path) as connection:
            updated = connection.execute(
                """
                UPDATE learning_jobs
                SET status = 'pending', leased_by = NULL, leased_at = NULL, updated_at = ?
                WHERE status = 'leased' AND (leased_at IS NULL OR leased_at <= ?)
                """,
                (now, stale_before),
            )
            connection.commit()
            return int(updated.rowcount or 0)

    def complete_learning_job(self, trace_id: str, report: dict[str, Any]) -> None:
        now = self._now()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = 'completed', updated_at = ?, leased_by = NULL, leased_at = NULL,
                    last_error = '', payload = ?
                WHERE trace_id = ?
                """,
                (now, json.dumps(to_jsonable(report), ensure_ascii=False), trace_id),
            )
            connection.commit()

    def fail_learning_job(
        self,
        trace_id: str,
        error: str,
        *,
        retry_delay_seconds: float,
        max_retry_delay_seconds: float,
        max_failures: int,
    ) -> str:
        job = self.learning_job(trace_id)
        if job is None:
            return "missing"
        now = self._now()
        attempts = int(job.get("attempts", 0))
        next_status = "pending" if attempts < max_failures else "dead_letter"
        backoff_seconds = min(retry_delay_seconds * max(1, 2 ** max(attempts - 1, 0)), max_retry_delay_seconds)
        available_at = self._shifted_time(backoff_seconds) if next_status == "pending" else now
        payload = dict(job.get("payload", {}))
        payload.update(
            {
                "last_failure_at": now,
                "last_backoff_seconds": round(backoff_seconds, 2),
                "last_error": error,
                "manual_recovery_required": next_status == "dead_letter",
            }
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = ?, available_at = ?, updated_at = ?, leased_by = NULL, leased_at = NULL, last_error = ?, payload = ?
                WHERE trace_id = ?
                """,
                (next_status, available_at, now, error, json.dumps(to_jsonable(payload), ensure_ascii=False), trace_id),
            )
            connection.commit()
        return next_status

    def learning_job_summary(self) -> dict[str, Any]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*)
                FROM learning_jobs
                GROUP BY status
                """
            ).fetchall()
            priorities = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('pending', 'leased') AND priority >= 70 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('pending', 'leased') AND priority >= 40 AND priority < 70 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('pending', 'leased') AND priority < 40 THEN 1 ELSE 0 END)
                FROM learning_jobs
                """
            ).fetchone()
        counts = {str(status): int(count) for status, count in rows}
        return {
            "pending": counts.get("pending", 0),
            "leased": counts.get("leased", 0),
            "completed": counts.get("completed", 0),
            "dead_letter": counts.get("dead_letter", 0),
            "dismissed": counts.get("dismissed", 0),
            "active": counts.get("pending", 0) + counts.get("leased", 0),
            "priority_buckets": {
                "high": int(priorities[0] or 0),
                "medium": int(priorities[1] or 0),
                "low": int(priorities[2] or 0),
            },
        }

    def learning_jobs(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = """
            SELECT *
            FROM learning_jobs
        """
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._learning_job_from_row(row) for row in rows]

    def retry_learning_job(
        self,
        trace_id: str,
        *,
        actor: str,
        reason: str,
        priority: int | None = None,
    ) -> dict[str, Any] | None:
        job = self.learning_job(trace_id)
        if job is None:
            return None
        now = self._now()
        payload = dict(job.get("payload", {}))
        actions = list(payload.get("manual_actions", []))
        actions.append({"action": "retry", "actor": actor, "reason": reason, "at": now})
        payload["manual_actions"] = actions[-10:]
        payload["manual_recovery_required"] = False
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = 'pending',
                    available_at = ?,
                    updated_at = ?,
                    leased_by = NULL,
                    leased_at = NULL,
                    attempts = 0,
                    last_error = '',
                    priority = ?,
                    payload = ?
                WHERE trace_id = ?
                """,
                (
                    now,
                    now,
                    priority if priority is not None else int(job.get("priority", 0)),
                    json.dumps(to_jsonable(payload), ensure_ascii=False),
                    trace_id,
                ),
            )
            connection.commit()
        return self.learning_job(trace_id)

    def dismiss_learning_job(self, trace_id: str, *, actor: str, reason: str) -> dict[str, Any] | None:
        job = self.learning_job(trace_id)
        if job is None:
            return None
        now = self._now()
        payload = dict(job.get("payload", {}))
        actions = list(payload.get("manual_actions", []))
        actions.append({"action": "dismiss", "actor": actor, "reason": reason, "at": now})
        payload["manual_actions"] = actions[-10:]
        payload["manual_recovery_required"] = False
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = 'dismissed',
                    updated_at = ?,
                    leased_by = NULL,
                    leased_at = NULL,
                    payload = ?
                WHERE trace_id = ?
                """,
                (now, json.dumps(to_jsonable(payload), ensure_ascii=False), trace_id),
            )
            connection.commit()
        return self.learning_job(trace_id)

    def acquire_service_lease(
        self,
        service_name: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        now = self._now()
        expires_at = self._shifted_time(ttl_seconds)
        payload_blob = json.dumps(to_jsonable(payload or {}), ensure_ascii=False)
        with sqlite3.connect(self.database_path) as connection:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT owner_id, lease_expires_at
                FROM service_leases
                WHERE service_name = ?
                """,
                (service_name,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO service_leases (service_name, owner_id, lease_expires_at, heartbeat_at, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (service_name, owner_id, expires_at, now, payload_blob),
                )
                connection.commit()
                return True
            current_owner, lease_expires_at = str(row[0]), str(row[1])
            if current_owner == owner_id or lease_expires_at <= now:
                connection.execute(
                    """
                    UPDATE service_leases
                    SET owner_id = ?, lease_expires_at = ?, heartbeat_at = ?, payload = ?
                    WHERE service_name = ?
                    """,
                    (owner_id, expires_at, now, payload_blob, service_name),
                )
                connection.commit()
                return True
            connection.commit()
        return False

    def service_lease(self, service_name: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT service_name, owner_id, lease_expires_at, heartbeat_at, payload
                FROM service_leases
                WHERE service_name = ?
                """,
                (service_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "service_name": row[0],
            "owner_id": row[1],
            "lease_expires_at": row[2],
            "heartbeat_at": row[3],
            "payload": json.loads(row[4]) if row[4] else {},
        }

    def unlearned_successful_trace_ids(self, limit: int = 10) -> list[str]:
        jobs_by_trace: dict[str, str] = {}
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT trace_id, status
                FROM learning_jobs
                """
            ).fetchall()
        for trace_id, status in rows:
            jobs_by_trace[str(trace_id)] = str(status)

        candidates: list[str] = []
        for trace in self.trace_payloads(limit=max(limit * 6, 30)):
            if not trace.get("success"):
                continue
            if trace.get("execution_mode") != "repair":
                continue
            trace_id = str(trace.get("id", ""))
            if not trace_id:
                continue
            trace_status = str(trace.get("auto_learning_status", "not_requested"))
            if jobs_by_trace.get(trace_id) in {"pending", "leased", "completed"}:
                continue
            if trace_status in {"queued", "running", "completed"}:
                continue
            candidates.append(trace_id)
            if len(candidates) >= limit:
                break
        return candidates

    def append_collaboration_memories(self, trace: ExecutionTrace) -> int:
        now = trace.created_at
        records: list[tuple[str, str, str, str, str, str, str, str]] = []
        for execution in trace.executions:
            payload = {
                "task_id": execution.task_id,
                "role": execution.role.value,
                "summary": execution.reasoning_summary,
                "step_titles": [step.title for step in execution.repair_steps[:4]],
                "focus_files": list(
                    dict.fromkeys(
                        file_path
                        for step in execution.repair_steps
                        for file_path in step.target_files
                        if file_path
                    )
                )[:6],
                "collaboration_messages": execution.metadata.get("collaboration_messages", []),
                "tool_calls": execution.metadata.get("tool_protocol_calls", []),
                "memory_write": execution.metadata.get("shared_memory_write", {}),
            }
            records.append(
                (
                    f"memory-{uuid4().hex}",
                    trace.id,
                    trace.compressed_error.error_type.value,
                    trace.compressed_error.root_cause_cluster,
                    execution.role.value,
                    "execution_summary",
                    execution.reasoning_summary[:320],
                    json.dumps(to_jsonable(payload), ensure_ascii=False),
                )
            )
        if trace.collaboration_blackboard:
            records.append(
                (
                    f"memory-{uuid4().hex}",
                    trace.id,
                    trace.compressed_error.error_type.value,
                    trace.compressed_error.root_cause_cluster,
                    "shared_blackboard",
                    "consensus",
                    str(trace.repair_summary)[:320],
                    json.dumps(to_jsonable(trace.collaboration_blackboard), ensure_ascii=False),
                )
            )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM collaboration_memories WHERE trace_id = ?", (trace.id,))
            connection.executemany(
                """
                INSERT INTO collaboration_memories (
                    memory_id, trace_id, incident_type, cluster, role, category, summary, created_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(item[0], item[1], item[2], item[3], item[4], item[5], item[6], now, item[7]) for item in records],
            )
            connection.commit()
        return len(records)

    def related_collaboration_memories(
        self,
        *,
        incident_type: str,
        cluster: str,
        keywords: list[str],
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        like_terms = [keyword.lower() for keyword in keywords[:3]]
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT memory_id, trace_id, incident_type, cluster, role, category, summary, created_at, payload
                FROM collaboration_memories
                WHERE incident_type = ? OR cluster = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (incident_type, cluster, limit * 3),
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            payload_text = row[8].lower()
            if row[3] == cluster or any(term in payload_text for term in like_terms):
                payload = json.loads(row[8])
                matches.append(
                    {
                        "memory_id": row[0],
                        "trace_id": row[1],
                        "incident_type": row[2],
                        "cluster": row[3],
                        "role": row[4],
                        "category": row[5],
                        "repair_summary": row[6],
                        "created_at": row[7],
                        **(payload if isinstance(payload, dict) else {"payload": payload}),
                    }
                )
            if len(matches) >= limit:
                break
        return matches

    def collaboration_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT memory_id, trace_id, incident_type, cluster, role, category, summary, created_at, payload
                FROM collaboration_memories
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row[8])
            candidates.append(
                {
                    "memory_id": row[0],
                    "trace_id": row[1],
                    "incident_type": row[2],
                    "cluster": row[3],
                    "role": row[4],
                    "category": row[5],
                    "repair_summary": row[6],
                    "created_at": row[7],
                    **(payload if isinstance(payload, dict) else {"payload": payload}),
                }
            )
        return candidates

    def _now(self) -> str:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()
        if row and row[0]:
            return str(row[0]).replace(" ", "T") + "Z"
        return ""

    def _shifted_time(self, delta_seconds: float) -> str:
        with sqlite3.connect(self.database_path) as connection:
            modifier = f"{delta_seconds:+f} seconds"
            row = connection.execute("SELECT datetime('now', ?)", (modifier,)).fetchone()
        if row and row[0]:
            return str(row[0]).replace(" ", "T") + "Z"
        return ""

    def _learning_job_from_row(self, row) -> dict[str, Any]:
        return {
            "trace_id": row[0],
            "priority": int(row[1]),
            "status": row[2],
            "available_at": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "leased_by": row[6],
            "leased_at": row[7],
            "attempts": int(row[8]),
            "last_error": row[9],
            "source": row[10],
            "payload": json.loads(row[11]) if row[11] else {},
        }

    def trace_payloads_by_ids(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for trace_id in trace_ids:
            payload = self.trace_payload(trace_id)
            if payload is not None:
                payloads.append(payload)
        return payloads

    def update_trace_payload(self, trace_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        payload = self.trace_payload(trace_id)
        if payload is None:
            return None
        payload.update(to_jsonable(updates))
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE traces
                SET payload = ?
                WHERE trace_id = ?
                """,
                (json.dumps(payload, ensure_ascii=False), trace_id),
            )
            connection.commit()
        return payload

    def trace_ids_for_incidents(self, incident_ids: list[str]) -> list[str]:
        ids = {str(item) for item in incident_ids if str(item).strip()}
        if not ids:
            return []
        trace_ids: list[str] = []
        for payload in self.trace_payloads(limit=1000):
            incident_id = str(payload.get("incident", {}).get("id", ""))
            if incident_id in ids and payload.get("id"):
                trace_ids.append(str(payload["id"]))
        return trace_ids

    def delete_traces_for_incidents(self, incident_ids: list[str]) -> int:
        trace_ids = self.trace_ids_for_incidents(incident_ids)
        if not trace_ids:
            return 0
        placeholders = ",".join("?" for _ in trace_ids)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(f"DELETE FROM traces WHERE trace_id IN ({placeholders})", trace_ids)
            connection.execute(f"DELETE FROM learning_jobs WHERE trace_id IN ({placeholders})", trace_ids)
            connection.execute(f"DELETE FROM collaboration_memories WHERE trace_id IN ({placeholders})", trace_ids)
            connection.commit()
        return len(trace_ids)
