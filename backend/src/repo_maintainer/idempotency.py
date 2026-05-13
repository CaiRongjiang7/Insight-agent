"""
幂等键管理模块（任务编排层）

确保相同的 PR/Commit 不会被重复处理，避免：
1. Webhook 重放导致的重复抽取
2. 网络抖动导致的重复提交
3. 并发触发导致的重复执行

幂等键格式：{repo}:{pr_id}:{sha}
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class IdempotencyStore:
    """
    幂等键存储（SQLite 实现）

    提供幂等键的注册、查询和过期清理功能。
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key TEXT PRIMARY KEY,
        trace_id TEXT,
        status TEXT NOT NULL DEFAULT 'processing',
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        expires_at TEXT,
        completed_at TEXT
    )
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(self._CREATE_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_status ON idempotency_keys (status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys (expires_at)"
            )

    @staticmethod
    def make_key(repo: str, pr_id: str, sha: str) -> str:
        """
        构造幂等键

        格式：{repo}:{pr_id}:{sha}
        示例：octocat/hello-world:42:abc1234
        """
        raw = f"{repo}:{pr_id}:{sha}"
        return raw

    @staticmethod
    def make_key_hash(repo: str, pr_id: str, sha: str) -> str:
        """
        构造哈希幂等键（用于长字符串场景）

        返回 SHA1 哈希前缀 + 可读前缀
        """
        raw = f"{repo}:{pr_id}:{sha}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        # 保留可读前缀用于调试
        repo_slug = repo.replace("/", "-")[:20]
        return f"{repo_slug}:{pr_id[:8]}:{digest}"

    def try_acquire(
        self,
        key: str,
        trace_id: str | None = None,
        ttl_hours: int = 24,
        payload: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """
        尝试获取幂等锁

        Returns:
            (acquired, existing_record)
            - (True, None)：首次处理，成功获取锁
            - (False, record)：已有记录，返回现有处理状态
        """
        now = _utc_now()
        expires_at = _utc_add_hours(now, ttl_hours)
        payload_str = json.dumps(payload or {}, ensure_ascii=False)

        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO idempotency_keys (key, trace_id, status, payload, created_at, expires_at)
                    VALUES (?, ?, 'processing', ?, ?, ?)
                    """,
                    (key, trace_id, payload_str, now, expires_at),
                )
            return True, None
        except sqlite3.IntegrityError:
            # 键已存在，查询现有记录
            existing = self._get(key)
            if existing:
                # 检查是否已过期（过期的可以重新处理）
                exp = existing.get("expires_at")
                if exp and exp < now:
                    # 已过期，删除并重新创建
                    with self._conn() as conn:
                        conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
                    return self.try_acquire(key, trace_id, ttl_hours, payload)
            return False, existing

    def complete(self, key: str, trace_id: str, success: bool = True) -> None:
        """标记幂等键处理完成"""
        now = _utc_now()
        status = "completed" if success else "failed"
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE idempotency_keys
                SET status = ?, trace_id = ?, completed_at = ?
                WHERE key = ?
                """,
                (status, trace_id, now, key),
            )

    def get_status(self, key: str) -> dict[str, Any] | None:
        """查询幂等键状态"""
        return self._get(key)

    def cleanup_expired(self) -> int:
        """清理已过期的幂等键，返回清理数量"""
        now = _utc_now()
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM idempotency_keys WHERE expires_at < ? AND status IN ('completed', 'failed')",
                (now,),
            )
            return cursor.rowcount

    def summary(self) -> dict[str, int]:
        """统计各状态的幂等键数量"""
        rows = self._conn().execute(
            "SELECT status, COUNT(*) as cnt FROM idempotency_keys GROUP BY status"
        ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    def _get(self, key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)


class IdempotencyManager:
    """
    幂等管理器（高级封装）

    提供基于 BugReport 的幂等检查便利方法。
    """

    def __init__(self, store: IdempotencyStore) -> None:
        self.store = store

    def check_bug_report(
        self,
        repo: str,
        pr_id: str,
        sha: str,
        trace_id: str | None = None,
        ttl_hours: int = 24,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        检查 Bug 报告是否已经处理过

        Returns:
            (is_new, idempotency_key, existing_record)
            - is_new=True：首次处理
            - is_new=False：已有记录（existing_record 包含状态信息）
        """
        key = self.store.make_key(repo, pr_id, sha)
        is_new, existing = self.store.try_acquire(
            key,
            trace_id=trace_id,
            ttl_hours=ttl_hours,
            payload={
                "repo": repo,
                "pr_id": pr_id,
                "sha": sha,
            },
        )
        return is_new, key, existing

    def mark_completed(self, key: str, trace_id: str, success: bool = True) -> None:
        """标记处理完成"""
        self.store.complete(key, trace_id, success)

    def cleanup(self) -> int:
        """清理过期记录"""
        return self.store.cleanup_expired()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_add_hours(base: str, hours: int) -> str:
    from datetime import timedelta
    dt = datetime.fromisoformat(base)
    return (dt + timedelta(hours=hours)).isoformat()
