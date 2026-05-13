"""
Knowledge base — stores team bug review entries indexed by keywords and triggers.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import CompressedError, BugReport, KnowledgeEntry, Skill, IncidentReport, iso_now


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeBase:
    """
    Knowledge base for team bug review entries.

    Supports add / update / delete / search of knowledge entries extracted
    from LLM bug reviews, with keyword + BM25 retrieval.
    """

    def __init__(self, skills_path: str | Path) -> None:
        self.skills_path = Path(skills_path)
        self.skills_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, KnowledgeEntry] = {}
        self._load()
        if not self._entries:
            self.bootstrap_defaults()

    def bootstrap_defaults(self) -> None:
        """Seed default knowledge entries for common bug categories."""
        defaults = [
            KnowledgeEntry(
                id="ke-react-closure-stale",
                name="React 闭包陷阱（Stale Closure）",
                description=(
                    "在 React hooks 中，useEffect / useCallback / setTimeout 等闭包捕获了旧的 state/props，"
                    "导致读取到过期数据。常见于事件监听器、定时器和异步回调中未正确声明依赖项。"
                ),
                triggers=["useEffect", "useState", "closure", "stale", "deps", "依赖数组", "闭包"],
                error_types=["react_closure", "cross_module_defect"],
                action_template=(
                    "检查闭包所在的 hooks 依赖数组是否完整声明了所有外部变量；"
                    "使用 useRef 保存最新值，或通过 functional update 避免直接读取 state；"
                    "使用 eslint-plugin-react-hooks 辅助检测遗漏依赖。"
                ),
                keywords=["react", "hook", "closure", "useEffect", "stale state"],
                applicable_context="前端",
            ),
            KnowledgeEntry(
                id="ke-sse-stream-error",
                name="SSE 流式响应异常处理",
                description=(
                    "Server-Sent Events 流中断后客户端未正确重连，或服务端未正确 flush 数据导致消息积压。"
                    "常见问题：Nginx 反向代理缓冲、EventSource 错误回调未处理、流关闭后资源泄漏。"
                ),
                triggers=["SSE", "EventSource", "stream", "flush", "nginx", "buffer", "reconnect"],
                error_types=["sse_stream"],
                action_template=(
                    "服务端设置 Content-Type: text/event-stream，关闭 Nginx 缓冲（X-Accel-Buffering: no）；"
                    "客户端监听 onerror 事件并在适当延迟后重连；"
                    "确保流结束时服务端发送 [DONE] 信号并关闭连接。"
                ),
                keywords=["sse", "server-sent events", "streaming", "nginx buffer", "reconnect"],
                applicable_context="全栈",
            ),
            KnowledgeEntry(
                id="ke-race-condition-async",
                name="异步竞态条件（Race Condition）",
                description=(
                    "多个异步操作并发执行，后发起的请求先返回导致数据覆盖（经典的搜索建议场景）；"
                    "或并发写操作未加锁导致数据不一致。"
                ),
                triggers=["race", "concurrent", "async", "await", "cancel", "abort", "竞态", "并发"],
                error_types=["race_condition"],
                action_template=(
                    "使用 AbortController 取消旧的 fetch 请求；"
                    "使用版本号/序列号标记请求，忽略过期响应；"
                    "后端写操作使用数据库事务或分布式锁保证原子性。"
                ),
                keywords=["race condition", "abort controller", "concurrency", "stale request"],
                applicable_context="全栈",
            ),
            KnowledgeEntry(
                id="ke-ws-reconnect",
                name="WebSocket 断线重连",
                description=(
                    "WebSocket 连接异常断开后客户端未实现自动重连，或重连逻辑缺乏指数退避导致服务端被大量重连请求击垮。"
                ),
                triggers=["WebSocket", "reconnect", "disconnect", "close", "heartbeat", "ping", "重连"],
                error_types=["ws_reconnect"],
                action_template=(
                    "实现指数退避重连策略（初始 1s，最大 30s）；"
                    "添加心跳检测（ping/pong）以主动感知连接断开；"
                    "在页面可见性变化（visibilitychange）时触发重连检查。"
                ),
                keywords=["websocket", "reconnect", "exponential backoff", "heartbeat"],
                applicable_context="前端",
            ),
            KnowledgeEntry(
                id="ke-ci-env-pollution",
                name="CI 环境污染",
                description=(
                    "CI 流水线中共享环境变量或全局状态被不同 Job 污染，导致用例在本地通过而 CI 失败；"
                    "或 CI 缓存未正确清理导致旧产物影响新构建。"
                ),
                triggers=["ci", "environment", "cache", "pollution", "flaky", "traceback", "modulenotfounderror"],
                error_types=["ci_pollution", "ci_failure"],
                action_template=(
                    "每个 Job 使用独立的环境隔离（Docker 容器或虚拟环境）；"
                    "显式清理 CI 缓存或使用内容寻址缓存键；"
                    "追踪第一个失败的栈帧，验证 import 路径正确性。"
                ),
                keywords=["ci", "environment pollution", "cache", "flaky test"],
                applicable_context="后端/DevOps",
            ),
        ]
        for entry in defaults:
            self._entries[entry.id] = entry
        self._save()

    def all_entries(self) -> list[KnowledgeEntry]:
        self._load()
        return sorted(self._entries.values(), key=lambda e: e.last_used_at or "", reverse=True)

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        self._load()
        return self._entries.get(entry_id)

    def add(self, entry: KnowledgeEntry) -> None:
        self._load()
        self._entries[entry.id] = entry
        self._save()

    def update(self, entry_id: str, updates: dict[str, object]) -> bool:
        self._load()
        existing = self._entries.get(entry_id)
        if existing is None:
            return False
        for key, value in updates.items():
            if hasattr(existing, key) and value is not None:
                setattr(existing, key, value)
        self._save()
        return True

    def remove(self, entry_id: str) -> bool:
        self._load()
        if entry_id not in self._entries:
            return False
        del self._entries[entry_id]
        self._save()
        return True

    def search_by_keyword(self, query: str, limit: int = 10) -> list[KnowledgeEntry]:
        """Simple keyword-based search (BM25-style scoring)."""
        self._load()
        tokens = [t.lower() for t in query.split() if len(t) > 1]
        if not tokens:
            return []
        scored: list[tuple[float, KnowledgeEntry]] = []
        for entry in self._entries.values():
            text = " ".join(
                [entry.name, entry.description, entry.applicable_context]
                + entry.triggers + entry.keywords + entry.error_types
            ).lower()
            score = sum(text.count(t) for t in tokens)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]

    def match_compressed_error(self, error: CompressedError) -> list[KnowledgeEntry]:
        """Match knowledge entries against a compressed error signature."""
        self._load()
        matched: list[KnowledgeEntry] = []
        error_text = " ".join(
            [error.error_type.value, error.error_name, *error.keywords]
        ).lower()
        for entry in self._entries.values():
            entry_text = " ".join(
                [entry.name, entry.description, *entry.triggers, *entry.error_types]
            ).lower()
            if any(kw in entry_text for kw in error.keywords) or error.error_type.value in entry.error_types:
                entry.hit_rate = min(1.0, entry.hit_rate + 0.05)
                entry.last_used_at = _utc_now().isoformat()
                matched.append(entry)
        return matched

    def retire(self, entry_id: str, reason: str = "") -> bool:
        self._load()
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.status = "retired"
        self._save()
        return True

    def restore(self, entry_id: str) -> bool:
        self._load()
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.status = "active"
        self._save()
        return True

    def _load(self) -> None:
        if not self.skills_path.exists():
            return
        try:
            data = json.loads(self.skills_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = []
        self._entries = {item["id"]: KnowledgeEntry.from_dict(item) for item in data}

    def _save(self) -> None:
        payload = [asdict(entry) for entry in self.all_entries()]
        self.skills_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class SkillRepository(KnowledgeBase):
    """Type alias for compatibility with legacy skill_repository API."""

    def all_skills(self) -> list[KnowledgeEntry]:
        """Legacy: same as all_entries()."""
        return self.all_entries()

    def upsert(self, entry: KnowledgeEntry) -> None:
        """Legacy: same as add()."""
        return self.add(entry)

    def retire_skill(self, skill_id: str, reason: str = "") -> bool:
        """Legacy: same as retire()."""
        return self.retire(skill_id, reason)

    def restore_skill(self, skill_id: str) -> bool:
        """Legacy: same as restore()."""
        return self.restore(skill_id)

    def update_skill(self, skill_id: str, updates: dict[str, object]) -> bool:
        """Legacy: same as update()."""
        return self.update(skill_id, updates)