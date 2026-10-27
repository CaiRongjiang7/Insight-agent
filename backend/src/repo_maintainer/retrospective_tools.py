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


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class RelatedCodeArgs(_ToolArgs):
    symbol: str = Field(min_length=1, max_length=300)
    direction: Literal["callers", "callees", "both"]
    limit: int = Field(ge=1, le=6)


class SearchContextArgs(_ToolArgs):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(ge=1, le=5)


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    status: Literal["ok", "empty", "ambiguous", "error", "budget_exhausted"]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    historical_cases: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
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
        timeout_seconds: float = 30.0,
    ) -> None:
        self.incident = incident
        self.code_session = code_session
        self.history_search = history_search
        self.persist_evidence = persist_evidence
        self.recorder = recorder
        self.max_calls = max(0, max_calls)
        self.output_budget_chars = max(0, output_budget_chars)
        self.deadline = time.monotonic() + max(0, timeout_seconds)
        self.calls_used = 0
        self.used_chars = 0
        self.added_evidence: list[Evidence] = []
        self.historical_hits: list[KnowledgeHit] = []
        self._raw = {e.id: e.model_copy(deep=True) for e in raw_evidence if e.incident_id == incident.id}
        self._cache: dict[str, ToolResult] = {}
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
            # A repeated result references already returned material; it does not
            # append the same bodies to the next model request again.
            result = ToolResult(
                status=previous.status, candidates=list(previous.candidates),
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
        return self._finish(name, normalized, result, started)

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
        return self._evidence_result(evidence[:args.limit], truncated=bool(getattr(lookup, "truncated", False)) or len(evidence) > args.limit)

    def _search_feishu(self, args: SearchContextArgs) -> ToolResult:
        terms = _query_terms(args.query)
        ranked = []
        for item in self._raw.values():
            if item.source_type not in {EvidenceSource.FEISHU_MESSAGE, EvidenceSource.FEISHU_DOC}:
                continue
            content = item.content.casefold()
            score = sum(min(len(term), 12) for term in terms if term in content)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        return self._evidence_result([item for _, item in ranked[:args.limit]],
                                     truncated=len(ranked) > args.limit, query_terms=terms)

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
            content, clipped, offset = self._take_body(item.content, 2_400, query_terms)
            if not content:
                result.truncated = True
                break
            role = {EvidenceSource.FEISHU_MESSAGE: "participant_statement",
                    EvidenceSource.FEISHU_DOC: "document_statement"}.get(item.source_type, "source_text")
            result.evidence.append({
                "evidence_id": item.id, "source_type": item.source_type.value, "material_role": role,
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
