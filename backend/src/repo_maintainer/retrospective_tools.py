"""Read-only Function Tools with server-owned scope and bounded results.

The tools retrieve material. They do not approve a conclusion, edit source code,
or publish a retrospective. Historical hits deliberately have no Evidence ID.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .retrospective_models import Evidence, EvidenceSource, IncidentRecord, KnowledgeHit
from .retrospective_evidence_policy import material_role


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class RelatedCodeArgs(_ToolArgs):
    symbol: str = Field(min_length=1, max_length=300)
    direction: Literal["callers", "callees", "both"]
    limit: int = Field(ge=1, le=6)


class SearchContextArgs(_ToolArgs):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(ge=1, le=5)


class FindFilesArgs(_ToolArgs):
    pattern: str = Field(min_length=1, max_length=160)
    limit: int = Field(ge=1, le=40)


class SearchCodeArgs(_ToolArgs):
    query: str = Field(min_length=1, max_length=160, pattern=r'^[^\r\n\x00]+$')
    file_pattern: str = Field(min_length=1, max_length=160)
    case_sensitive: bool
    limit: int = Field(ge=1, le=20)


class ReadFileArgs(_ToolArgs):
    path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(ge=1, le=1_000_000)
    end_line: int = Field(ge=1, le=1_000_000)


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    status: Literal["ok", "empty", "ambiguous", "error", "budget_exhausted"]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    historical_cases: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    locations: list[dict[str, Any]] = Field(default_factory=list)
    search_scope: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    error: ToolError | None = None
    cache_hit: bool = False
    reused_evidence_ids: list[str] = Field(default_factory=list)
    reused_knowledge_ids: list[str] = Field(default_factory=list)


class ToolPersistenceError(RuntimeError):
    """Original material could not be saved; do not present it as retrieved."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], ToolResult]

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function", "name": self.name, "description": self.description,
            "parameters": self.input_model.model_json_schema(), "strict": True,
        }


class ToolExecutor:
    """One investigation's registry, cache and result budget.

    ``history_search`` is a server-bound callback: it must inject the incident's
    repository/type filters. Time is checked between synchronous calls; existing
    connector timeouts still apply inside a history search.
    """

    def __init__(
        self,
        incident: IncidentRecord,
        raw_evidence: list[Evidence],
        *,
        code_session=None,
        history_search: Callable[[str, int], list[KnowledgeHit]] | None = None,
        persist_evidence: Callable[[list[Evidence]], Any] | None = None,
        recorder: Callable[[dict[str, Any]], Any] | None = None,
        max_calls: int = 4,
        output_budget_chars: int = 8_000,
        item_chars: int = 2_400,
        timeout_seconds: float = 30.0,
        repository_search_enabled: bool = False,
    ) -> None:
        self.incident = incident
        self.code_session = code_session
        self.history_search = history_search
        self.persist_evidence = persist_evidence
        self.recorder = recorder
        self.max_calls = max(0, max_calls)
        self.output_budget_chars = max(0, output_budget_chars)
        self.item_chars = max(500, item_chars)
        self.deadline = time.monotonic() + max(0, timeout_seconds)
        self.calls_used = 0
        self.used_chars = 0
        self.added_evidence: list[Evidence] = []
        self.historical_hits: list[KnowledgeHit] = []
        self._raw = {e.id: e.model_copy(deep=True) for e in raw_evidence if e.incident_id == incident.id}
        self._cache: dict[str, ToolResult] = {}
        self._cache_delivered: set[str] = set()
        self._served_evidence: set[str] = set()
        self._served_history: set[str] = set()
        self.specs = {
            "get_related_code": ToolSpec(
                "get_related_code",
                "读取当前事故固定代码版本中的唯一方法及直接调用者/被调用者；歧义时返回候选，不推断运行事实。",
                RelatedCodeArgs, self._get_related_code,
            ),
            "search_feishu_context": ToolSpec(
                "search_feishu_context",
                "在本次事故已采集的授权群聊和配置文档内检索；内容属于参与者或文档陈述。",
                SearchContextArgs, self._search_feishu,
            ),
            "search_historical_cases": ToolSpec(
                "search_historical_cases",
                "在服务端绑定的仓库/问题类型内查询已发布历史案例；仅供类比，不是当前事故证据。",
                SearchContextArgs, self._search_history,
            ),
        }
        if repository_search_enabled:
            self.specs.update({
                'find_repository_files': ToolSpec('find_repository_files',
                    '在固定 SHA 的已索引文件中按相对路径通配符找文件；* 可匹配路径分隔符。仅导航，不是影响证据。',
                    FindFilesArgs, self._find_files),
                'search_repository_code': ToolSpec('search_repository_code',
                    '在固定 SHA 内做单行字面量搜索（类似 grep -F，不支持正则）；返回有界路径/行号，随后用 read_repository_file 取可引用原文。未命中不证明未受影响。',
                    SearchCodeArgs, self._search_code),
                'read_repository_file': ToolSpec('read_repository_file',
                    f'读取已索引相对路径的 1-based 闭区间，最多120行/{self.item_chars}字符（并受源码区域上限约束）并保存原文；源码、配置和文档仅支持静态候选，不证明本次运行影响。',
                    ReadFileArgs, self._read_file),
            })

    def definitions(self) -> list[dict[str, Any]]:
        return [spec.definition() for spec in self.specs.values()]

    def execute(self, name: str, arguments: str | dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        spec = self.specs.get(name)
        if spec is None:
            return self._finish(name, {}, self._error("UNKNOWN_TOOL", "Tool is not registered"), started)
        try:
            if isinstance(arguments, str):
                if len(arguments) > 12_000:
                    raise ValueError("Arguments are too large")
                parsed = spec.input_model.model_validate_json(arguments)
            else:
                parsed = spec.input_model.model_validate(arguments)
        except (ValidationError, ValueError, TypeError):
            return self._finish(name, {}, self._error("INVALID_ARGUMENTS", "Arguments do not match the tool schema"), started)
        normalized = parsed.model_dump(mode="json")
        cache_key = name + ":" + json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        if cache_key in self._cache:
            previous = self._cache[cache_key]
            if cache_key not in self._cache_delivered:
                self._cache_delivered.add(cache_key)
                return self._finish(name, normalized, self._replay_cached(previous), started)
            # A repeated result references already returned material; it does not
            # append the same bodies to the next model request again.
            result = ToolResult(
                status=previous.status, candidates=list(previous.candidates),
                search_scope=dict(previous.search_scope),
                truncated=previous.truncated, error=previous.error, cache_hit=True,
                reused_evidence_ids=[e["evidence_id"] for e in previous.evidence] + previous.reused_evidence_ids,
                reused_knowledge_ids=[h["knowledge_id"] for h in previous.historical_cases] + previous.reused_knowledge_ids,
            )
            return self._finish(name, normalized, result, started)
        if self.calls_used >= self.max_calls or self.used_chars >= self.output_budget_chars or time.monotonic() >= self.deadline:
            return self._finish(name, normalized, ToolResult(
                status="budget_exhausted", error=ToolError(code="BUDGET_EXHAUSTED", message="Investigation budget exhausted")), started)
        self.calls_used += 1
        try:
            result = spec.handler(parsed)
        except ToolPersistenceError:
            raise
        except Exception as exc:
            # Avoid returning credentials or provider response bodies in messages.
            result = self._error("TOOL_FAILED", f"Tool failed: {type(exc).__name__}", retryable=True)
        self._cache[cache_key] = result.model_copy(deep=True)
        self._cache_delivered.add(cache_key)
        return self._finish(name, normalized, result, started)

    def on_compaction(self) -> None:
        """A native checkpoint changed visible history, not source/cache identity."""
        self._cache_delivered.clear()
        self._served_evidence.clear()
        self._served_history.clear()

    def _replay_cached(self, previous: ToolResult) -> ToolResult:
        # Rehydrate an already retrieved window once in the new context. Do not
        # re-fetch external material, spend another region or duplicate originals.
        if time.monotonic() >= self.deadline or self.used_chars >= self.output_budget_chars:
            return ToolResult(status='budget_exhausted', cache_hit=True, error=ToolError(
                code='BUDGET_EXHAUSTED', message='Cached window delivery budget exhausted'))
        result = previous.model_copy(deep=True)
        result.cache_hit = True
        for field, id_key, reused_key in [('evidence', 'evidence_id', 'reused_evidence_ids'),
                                         ('historical_cases', 'knowledge_id', 'reused_knowledge_ids')]:
            items = {item[id_key]: item for item in getattr(result, field)}
            for reused_id in getattr(previous, reused_key):
                for cached in self._cache.values():
                    original = next((item for item in getattr(cached, field) if item[id_key] == reused_id), None)
                    if original is not None:
                        items.setdefault(reused_id, dict(original))
                        break
            delivered = []
            for item in items.values():
                body, clipped, _ = self._take_body(item['content'], self.item_chars if field == 'evidence' else 1500)
                if not body:
                    result.truncated = True
                    break
                item['content'] = body
                item['truncated'] |= clipped
                if clipped and field == 'evidence': item['region_complete'] = False
                result.truncated |= clipped
                delivered.append(item)
            setattr(result, field, delivered)
        result.locations = []
        for location in previous.locations:
            size = len(json.dumps(location, ensure_ascii=False))
            if self.used_chars + size > self.output_budget_chars:
                result.truncated = True
                break
            self.used_chars += size
            result.locations.append(dict(location))
        return result

    def _finish(self, name, arguments, result, started) -> ToolResult:
        if self.recorder is not None:
            # Persistence failures propagate to the orchestration layer. Do not
            # call the recorder again or relabel them as a retrieval failure.
            self.recorder({
                "name": name, "arguments": arguments,
                "result": result.model_dump(mode="json"), "cache_hit": result.cache_hit,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "calls_used": self.calls_used, "used_chars": self.used_chars,
            })
        return result

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool = False) -> ToolResult:
        return ToolResult(status="error", error=ToolError(code=code, message=message[:200], retryable=retryable))

    def _get_related_code(self, args: RelatedCodeArgs) -> ToolResult:
        if self.code_session is None:
            return self._error("CODE_UNAVAILABLE", "Pinned repository snapshot is unavailable")
        lookup = self.code_session.get_related_code(args.symbol, args.direction, args.limit)
        return self._code_result(lookup, args.limit)

    def _read_file(self, args: ReadFileArgs) -> ToolResult:
        if self.code_session is None:
            return self._error('CODE_UNAVAILABLE', 'Pinned repository snapshot is unavailable')
        if args.end_line < args.start_line or args.end_line - args.start_line >= 120:
            return self._error('INVALID_ARGUMENTS', 'Read at most 120 lines in ascending order')
        try:
            lookup = self.code_session.read_file(args.path, args.start_line, args.end_line)
        except ValueError:
            return self._error('INVALID_PATH', 'Path is outside the indexed snapshot')
        return self._code_result(lookup, 1)

    def _find_files(self, args: FindFilesArgs) -> ToolResult:
        return self._navigate(lambda: self.code_session.find_files(args.pattern, args.limit))

    def _search_code(self, args: SearchCodeArgs) -> ToolResult:
        return self._navigate(lambda: self.code_session.search_code(
            args.query, args.file_pattern, args.case_sensitive, args.limit,
            min(self.deadline, time.monotonic() + 3)))

    def _navigate(self, lookup: Callable) -> ToolResult:
        if self.code_session is None:
            return self._error('CODE_UNAVAILABLE', 'Pinned repository snapshot is unavailable')
        try:
            found = lookup()
        except ValueError:
            return self._error('INVALID_PATH', 'Use an indexed repository-relative path pattern')
        scope = {**found['search_scope'], 'repo_id': self.incident.repo_id,
                 'commit_sha': str(self.incident.metadata.get('head_sha') or self.incident.commit_sha),
                 'material_role': 'static_candidate', 'navigation_only': True,
                 'impact_coverage': 'unknown'}
        result = ToolResult(status='empty', truncated=found['truncated'], search_scope=scope)
        for location in found['locations']:
            # Navigation bodies share the same budget as source/chat/history.
            size = len(json.dumps(location, ensure_ascii=False))
            if self.used_chars + size > self.output_budget_chars:
                result.truncated = True
                result.search_scope['scan_completed'] = False
                break
            self.used_chars += size
            result.locations.append(location)
        if result.locations:
            result.status = 'ok'
        elif found['locations']:
            result.status = 'budget_exhausted'
        return result

    def _code_result(self, lookup, limit: int) -> ToolResult:
        status = getattr(lookup, "status", "error")
        candidates = [str(item)[:300] for item in getattr(lookup, "candidates", [])[:6]]
        if status == "ambiguous":
            return ToolResult(status="ambiguous", candidates=candidates, truncated=bool(getattr(lookup, "truncated", False)))
        if status in {"not_found", "empty"}:
            return ToolResult(status="empty")
        if status == "budget_exhausted":
            return ToolResult(status="budget_exhausted", truncated=True,
                              error=ToolError(code="CODE_BUDGET_EXHAUSTED", message="Repository region budget exhausted"))
        if status != "ok":
            return self._error("CODE_UNAVAILABLE", "Pinned code lookup could not be completed")
        evidence = list(getattr(lookup, "evidence", []))
        expected_sha = str(self.incident.metadata.get("head_sha") or self.incident.commit_sha)
        if any(not isinstance(e, Evidence) or e.incident_id != self.incident.id
               or e.source_type != EvidenceSource.REPOSITORY_CODE
               or e.repo_id != self.incident.repo_id or e.commit_sha != expected_sha for e in evidence):
            return self._error("INVALID_RESULT_SCOPE", "Code lookup returned material outside the incident snapshot")
        new = []
        for item in evidence:
            old = self._raw.get(item.id)
            if old is not None and (old.content != item.content or old.location != item.location or old.commit_sha != item.commit_sha):
                return self._error("EVIDENCE_ID_CONFLICT", "Evidence ID conflicts with existing original material")
            if old is None:
                new.append(item.model_copy(deep=True))
        # Persist originals before constructing clipped tool-result copies.
        if new and self.persist_evidence is not None:
            try:
                self.persist_evidence(new)
            except Exception as exc:
                raise ToolPersistenceError('Unable to persist original tool evidence') from exc
        for item in new:
            self._raw[item.id] = item.model_copy(deep=True)
            self.added_evidence.append(item.model_copy(deep=True))
        return self._evidence_result(evidence[:limit], truncated=bool(getattr(lookup, "truncated", False)) or len(evidence) > limit)

    def _search_feishu(self, args: SearchContextArgs) -> ToolResult:
        terms = _query_terms(args.query)
        ranked = []
        eligible = [item for item in self._raw.values()
                    if item.source_type in {EvidenceSource.FEISHU_MESSAGE, EvidenceSource.FEISHU_DOC}]
        scope = {'eligible_materials': len(eligible), 'external_fetch': False,
                 'impact_coverage': 'unknown', 'source_scope': 'already_collected_incident_chat_and_docs'}
        if not eligible:
            scope['availability'] = 'no_collected_material'
            scope['retry_with_new_query'] = False
            scope['note'] = '本次没有已采集的群聊或文档；此工具不实时连接飞书。改写查询不能增加素材，请保留缺口并停止对此来源重复查询。'
            return ToolResult(status='empty', search_scope=scope)
        for item in eligible:
            content = item.content.casefold()
            score = sum(min(len(term), 12) for term in terms if term in content)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        result = self._evidence_result([item for _, item in ranked[:args.limit]],
                                       truncated=len(ranked) > args.limit, query_terms=terms)
        result.search_scope = scope
        return result

    def _search_history(self, args: SearchContextArgs) -> ToolResult:
        if self.history_search is None:
            return self._error("HISTORY_UNAVAILABLE", "Historical search is unavailable")
        hits = self.history_search(args.query, args.limit)
        hits = [hit for hit in hits if isinstance(hit, KnowledgeHit) and hit.incident_id != self.incident.id]
        result = ToolResult(status="empty", truncated=len(hits) > args.limit)
        known = {hit.knowledge_id for hit in self.historical_hits}
        for hit in hits[:args.limit]:
            if hit.knowledge_id in self._served_history:
                result.reused_knowledge_ids.append(hit.knowledge_id)
                continue
            content, clipped, offset = self._take_body(hit.content, 1_500, _query_terms(args.query))
            if not content:
                result.truncated = True
                break
            result.historical_cases.append({
                "knowledge_id": hit.knowledge_id, "historical_incident_id": hit.incident_id,
                "material_role": "historical_analogy", "title": hit.title[:200], "content": content,
                "source_url": hit.source_url[:1000], "score": hit.score,
                "matched_section": str(hit.metadata.get("matched_section", ""))[:80], "truncated": clipped,
                "excerpt_start": offset,
            })
            result.truncated |= clipped
            self._served_history.add(hit.knowledge_id)
            if hit.knowledge_id not in known:
                self.historical_hits.append(hit.model_copy(deep=True, update={
                    'content': content, 'metadata': {**hit.metadata, 'tool_excerpt_start': offset},
                }))
                known.add(hit.knowledge_id)
        if result.historical_cases or result.reused_knowledge_ids:
            result.status = "ok"
        elif hits and self.used_chars >= self.output_budget_chars:
            result.status = "budget_exhausted"
        return result

    def _take_body(self, content: str, item_limit: int, query_terms: set[str] | None = None) -> tuple[str, bool, int]:
        allowed = min(item_limit, self.output_budget_chars - self.used_chars)
        offset = 0
        if allowed > 0 and len(content) > allowed and query_terms:
            lowered = content.casefold()
            # Prefer the longest available query term, then its first occurrence.
            matched = [(len(term), lowered.find(term)) for term in query_terms if term in lowered]
            if matched:
                _, anchor = max(matched, key=lambda pair: (pair[0], -pair[1]))
                offset = min(max(0, anchor - allowed // 4), max(0, len(content) - allowed))
        body = content[offset:offset + max(0, allowed)]
        self.used_chars += len(body)
        return body, len(body) < len(content), offset

    def _evidence_result(self, evidence: list[Evidence], *, truncated: bool = False,
                         query_terms: set[str] | None = None) -> ToolResult:
        result = ToolResult(status="empty", truncated=truncated)
        seen: set[str] = set()
        for item in evidence:
            if item.id in seen:
                continue
            seen.add(item.id)
            if item.id in self._served_evidence:
                result.reused_evidence_ids.append(item.id)
                continue
            content, clipped, offset = self._take_body(item.content, self.item_chars, query_terms)
            if not content:
                result.truncated = True
                break
            role = material_role(item.source_type)
            result.evidence.append({
                "evidence_id": item.id, "source_type": item.source_type.value, "material_role": role,
                'impact_coverage': 'unknown',
                "location": item.location[:500], "source_url": item.source_url[:1000],
                "commit_sha": item.commit_sha, "content": content, "truncated": clipped,
                "excerpt_start": offset,
                "region_complete": False if clipped else item.metadata.get("region_complete"),
            })
            result.truncated |= clipped
            self._served_evidence.add(item.id)
        if result.evidence or result.reused_evidence_ids:
            result.status = "ok"
        elif evidence and self.used_chars >= self.output_budget_chars:
            result.status = "budget_exhausted"
        return result


def _query_terms(query: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_]+", query.casefold()))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", query):
        terms.add(phrase)
        terms.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return terms
