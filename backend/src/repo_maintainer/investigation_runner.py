"""Investigation execution port; evidence ownership stays in the application."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .retrospective_models import Evidence, IncidentRecord, KnowledgeHit, new_id
from .retrospective_store import record_generation_context
from .retrospective_tools import ToolExecutor
from .retrospective_evidence_policy import material_role


@dataclass
class InvestigationContext:
    incident: IncidentRecord
    raw_evidence: list[Evidence]
    selected: list[Evidence]
    history: list[KnowledgeHit]
    store: Any
    generator: Any
    history_search: Callable
    code_session: Any = None


class InvestigationRunner(Protocol):
    def supports(self, generator: Any) -> bool: ...
    def run(self, context: InvestigationContext) -> tuple: ...


class InvestigationSession:
    """One caller-thread-owned executor and evidence ledger for either runtime."""

    def __init__(self, context: InvestigationContext, *, runtime: str = "builtin",
                 timeout_seconds: float = 30.0, max_calls: int = 4,
                 output_budget_chars: int = 8_000, repository_search_enabled: bool = False,
                 tool_item_chars: int = 2_400):
        self.context = context
        self.runtime = runtime
        self.run_id = new_id("toolrun")
        self.call_id = ""
        self.bridge_ids: set[str] = set()
        self.requested: dict[str, str] = {}
        self.outcomes: list[dict] = []
        self.executor = ToolExecutor(
            context.incident, context.raw_evidence, code_session=context.code_session,
            history_search=context.history_search, persist_evidence=context.store.add_evidence,
            recorder=self._record_tool, timeout_seconds=timeout_seconds,
            max_calls=max_calls, output_budget_chars=output_budget_chars,
            item_chars=tool_item_chars,
            repository_search_enabled=repository_search_enabled,
        )

    def record(self, purpose, request, **details):
        record_generation_context(self.context.store, self.context.incident, purpose, request,
                                  run_id=self.run_id, **details)

    def _record_tool(self, event):
        self.record("tool_execution", event, call_id=self.call_id, scope="tool_result")
        for item in event["result"].get("evidence", []):
            self.requested[item["evidence_id"]] = item["content"]
        self.outcomes.append({"tool": event["name"], "status": event["result"]["status"],
                              "error": (event["result"].get("error") or {}).get("code", ""),
                              "truncated": event['result']['truncated'],
                              "search_scope": event['result'].get('search_scope', {})})

    def execute(self, name, arguments, call_id):
        self.call_id = call_id
        try:
            return self.executor.execute(name, arguments)
        finally:
            self.call_id = ""

    def finish(self, stop_reason, rounds=0, error_type="", **details):
        summary = {
            "run_id": self.run_id, "runtime": self.runtime, "rounds": rounds,
            "calls_used": self.executor.calls_used, "output_chars": self.executor.used_chars,
            "stop_reason": stop_reason, "requested_evidence_ids": list(self.requested),
            "error_type": error_type, "tool_outcomes": self.outcomes, **details,
            "impact_coverage": "unknown", "max_calls": self.executor.max_calls,
            "output_budget_chars": self.executor.output_budget_chars,
        }
        self.record("tool_exploration_summary", summary, scope="exploration_summary")
        return self.executor.added_evidence, self.executor.historical_hits, self.requested, summary


def investigation_input(context: InvestigationContext, *, budget_chars: int = 8_000,
                        item_chars: int = 1_000, max_items: int = 12) -> dict:
    """Source excerpts for exploration; raw originals and writer selection remain separate."""
    remaining, evidence = budget_chars, []
    for item in context.selected[:max_items]:
        if remaining <= 0:
            break
        content = item.content[:min(item_chars, remaining)]
        evidence.append({"evidence_id": item.id, "source_type": item.source_type.value,
                         "material_role": material_role(item.source_type), "impact_coverage": "unknown",
                         "location": item.location, "content": content,
                         "truncated": len(content) < len(item.content)})
        remaining -= len(content)
    return {
        "incident": {"id": context.incident.id, "title": context.incident.title[:300],
                     "description": context.incident.description[:1_000]},
        "review_feedback": context.incident.metadata.get("review_feedback"),
        "evidence": evidence,
        "historical_cases": [{"knowledge_id": h.knowledge_id, "title": h.title,
                              "content": h.content[:800]} for h in context.history[:2]],
    }


class StandardInvestigationRunner:
    """The existing two-round protocol, retained as the default and rollback path."""

    def supports(self, generator):
        return all(callable(getattr(generator, name, None))
                   for name in ("build_tool_request", "request_tool_calls"))

    def run(self, context):
        session = InvestigationSession(context)
        messages, stop_reason, rounds, error_type = [], "round_limit", 0, ""
        for round_index in range(2):
            try:
                request = context.generator.build_tool_request(
                    context.incident, context.selected, context.history, messages,
                    session.executor.definitions())
            except Exception as exc:
                stop_reason, error_type = "model_error", type(exc).__name__
                break
            session.record("tool_exploration", request, round=round_index + 1, scope="provider_request")
            rounds += 1
            try:
                response = context.generator.request_tool_calls(request)
                output = response.get("output", []) if isinstance(response, dict) else response.output
                items = [item if isinstance(item, dict) else
                         item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump")
                         else vars(item) for item in output]
                items = json.loads(json.dumps(items, ensure_ascii=False))
                calls = [item for item in items if item.get("type") == "function_call"]
                call_ids = [item.get("call_id") for item in calls]
                if any(not isinstance(cid, str) or not cid for cid in call_ids) or len(set(call_ids)) != len(call_ids):
                    raise ValueError("Tool response has missing or duplicate call IDs")
                if any(not isinstance(item.get("name"), str) for item in calls):
                    raise ValueError("Tool response has an invalid function name")
            except Exception as exc:
                stop_reason, error_type = "model_error", type(exc).__name__
                break
            session.record("tool_model_response", {"output": items}, round=round_index + 1, scope="model_output")
            messages = [*request["input"], *items]
            if not calls:
                stop_reason = "no_tool_calls"
                break
            for call in calls:
                result = session.execute(call["name"], call.get("arguments", {}), call["call_id"])
                messages.append({"type": "function_call_output", "call_id": call["call_id"],
                                 "output": result.model_dump_json()})
        return session.finish(stop_reason, rounds, error_type)
