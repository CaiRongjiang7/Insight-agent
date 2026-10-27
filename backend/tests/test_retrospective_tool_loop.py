"""Contract tests for model-requested evidence tools, without external services."""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

from repo_maintainer.retrospective_models import (
    Citation,
    Evidence,
    EvidenceSource,
    IncidentCreate,
    IncidentStatus,
    KnowledgeHit,
)
from repo_maintainer.retrospective_pipeline import ContextSelector, PipelineError, RetrospectivePipeline
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore
from test_retrospective_pipeline import FakeCollaboration, FakeEmbedder, FakeGenerator, FakeGit


def function_call(call_id: str, name: str, arguments: dict, *, as_object=False):
    value = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }
    return SimpleNamespace(**value) if as_object else value


def returned_tools(request: dict) -> dict[str, dict]:
    result = {}
    for item in request.get("input", []):
        if not isinstance(item, dict):
            item = vars(item)
        if item.get("type") == "function_call_output":
            result[item["call_id"]] = json.loads(item["output"])
    return result


class FakeRepositorySession:
    def __init__(self, owner, incident):
        self.owner = owner
        self.incident = incident

    def collect(self, evidence, *, allow_planner=True):
        self.owner.collect_options.append(allow_planner)
        return []

    def get_related_code(self, symbol, direction="both", limit=6):
        self.owner.tool_calls.append((symbol, direction, limit))
        evidence = Evidence(
            id=f"ev_tool_restore_{self.incident.id}",
            incident_id=self.incident.id,
            source_type=EvidenceSource.REPOSITORY_CODE,
            content="def restore_registration(client):\n    client.register_instance()",
            location="sdk/recovery.py#L1-L2",
            source_url=f"https://github.com/team/sdk/blob/{self.incident.commit_sha}/sdk/recovery.py#L1-L2",
            repo_id=self.incident.repo_id,
            commit_sha=self.incident.commit_sha,
            metadata={"file_path": "sdk/recovery.py", "region_complete": True},
        )
        return SimpleNamespace(status="ok", evidence=[evidence], candidates=[], truncated=False, error="")


class FakeCodeContext:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.static_calls = 0
        self.collect_options = []
        self.tool_calls = []

    @contextmanager
    def open_session(self, incident):
        self.opens += 1
        try:
            yield FakeRepositorySession(self, incident)
        finally:
            self.closes += 1

    def collect(self, incident, evidence):
        self.static_calls += 1
        return []


class ToolLoopGenerator(FakeGenerator):
    def __init__(self, responses=(), *, cite_code=False, cite_history=False, invalid_first=False):
        self.responses = list(responses)
        self.requests = []
        self.final_inputs = []
        self.cite_code = cite_code
        self.cite_history = cite_history
        self.invalid_first = invalid_first

    def build_tool_request(self, incident, evidence, history, messages, tools):
        return {
            "model": "fake-evidence-tools",
            "input": deepcopy(messages) if messages else [{"role": "user", "content": incident.title}],
            "tools": deepcopy(tools),
            "store": False,
        }

    def request_tool_calls(self, request):
        self.requests.append(deepcopy(request))
        index = len(self.requests) - 1
        response = self.responses[index] if index < len(self.responses) else []
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(output=response)

    def generate(self, incident, evidence, history):
        self.final_inputs.append((incident.model_copy(deep=True), deepcopy(evidence), deepcopy(history)))
        document, metadata = super().generate(incident, evidence, history)
        if self.cite_code:
            code = next((item for item in evidence if item.id.startswith("ev_tool_restore_")), None)
            if code:
                document.fix_solution.citations = [Citation(evidence_id=code.id, quote="client.register_instance()")]
        if self.cite_history and history:
            document.fix_solution.citations = [Citation(evidence_id=history[0].knowledge_id, quote="old case guidance")]
        if self.invalid_first and not incident.metadata.get("generation_validation_errors"):
            document.root_cause.citations[0].quote = "fabricated quotation"
        return document, metadata


class ScopedHistoryStore(InMemoryRetrospectiveStore):
    def __init__(self):
        super().__init__()
        self.queries = []

    def search_knowledge(self, query, embedding, *, repo_id="", incident_type="", limit=5):
        self.queries.append((query, repo_id, incident_type, limit))
        return [KnowledgeHit(
            knowledge_id="knowledge_old_case", incident_id="old_incident", title="old registration case",
            content="old case guidance", score=0.8, source_url="https://feishu.test/wiki/old",
        )]


class RetrospectiveToolLoopTests(unittest.TestCase):
    def pipeline(self, generator=None, context=None, store=None, *, tools_enabled=True):
        return RetrospectivePipeline(
            store=store or InMemoryRetrospectiveStore(), git=FakeGit(), collaboration=FakeCollaboration(),
            generator=generator or ToolLoopGenerator(), embedder=FakeEmbedder(), code_context=context,
            selector=ContextSelector(budget_chars=6000, max_item_chars=2000), tools_enabled=tools_enabled,
        )

    def incident(self, pipeline):
        return pipeline.create_incident(IncidentCreate(
            title="upload registration recovery", repo_id="team/sdk", repo_name="team/sdk",
            commit_sha="a" * 40, incident_type="bug_fix",
        ))

    def purposes(self, pipeline, incident_id):
        return [entry.payload.get("purpose") for entry in pipeline.store.list_generation_contexts(incident_id)]

    def test_two_round_tool_results_feed_next_decision_and_new_code_citation_is_valid(self):
        generator = ToolLoopGenerator([
            [function_call("code", "get_related_code", {"symbol": "Client.watcher", "direction": "both", "limit": 3})],
            [function_call("chat", "search_feishu_context", {"query": "上传参数校验缺失", "limit": 2}, as_object=True)],
        ], cite_code=True)
        context = FakeCodeContext()
        pipeline = self.pipeline(generator, context)
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)

        self.assertEqual(len(generator.requests), 2)
        first_result = returned_tools(generator.requests[1])["code"]
        self.assertEqual(first_result["status"], "ok")
        self.assertIn("client.register_instance()", first_result["evidence"][0]["content"])
        self.assertTrue(version.quality.hard_passed)
        new_id = f"ev_tool_restore_{incident.id}"
        self.assertEqual(version.document.fix_solution.citations[0].evidence_id, new_id)
        self.assertIn(new_id, version.evidence_ids)
        self.assertTrue(any(item.id == new_id and item.selected for item in pipeline.store.list_evidence(incident.id)))
        self.assertEqual((context.opens, context.closes), (1, 1))
        self.assertEqual(context.collect_options, [False])
        self.assertEqual(len(context.tool_calls), 1)
        self.assertEqual(pipeline.store.get_incident(incident.id).status, IncidentStatus.PENDING_REVIEW)
        contexts = pipeline.store.list_generation_contexts(incident.id)
        for purpose in ("tool_exploration", "tool_execution", "tool_exploration_summary", "retrospective"):
            self.assertIn(purpose, self.purposes(pipeline, incident.id))
        runs = {entry.payload.get("run_id") for entry in contexts if entry.payload.get("purpose", "").startswith("tool_")}
        self.assertEqual(len(runs), 1)
        self.assertNotIn(None, runs)

    def test_model_failure_falls_back_and_closes_repository_session(self):
        generator = ToolLoopGenerator([TimeoutError("fake timeout")])
        context = FakeCodeContext()
        pipeline = self.pipeline(generator, context)
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual((context.opens, context.closes), (1, 1))
        self.assertEqual(len(generator.requests), 1)
        self.assertIn("tool_exploration", self.purposes(pipeline, incident.id))
        self.assertIn("tool_exploration_summary", self.purposes(pipeline, incident.id))
        self.assertEqual(len(generator.final_inputs), 1)

    def test_malformed_call_protocol_falls_back_without_executing_tools(self):
        duplicate = [function_call('same', 'search_feishu_context', {'query': '上传', 'limit': 1})] * 2
        invalid_name = function_call('invalid', 'search_feishu_context', {'query': '上传', 'limit': 1})
        invalid_name['name'] = ['not-a-function-name']
        for output in (duplicate, [invalid_name]):
            with self.subTest(output=output):
                generator = ToolLoopGenerator([output])
                context = FakeCodeContext()
                pipeline = self.pipeline(generator, context)
                incident = self.incident(pipeline)
                version = pipeline.run(incident.id)
                self.assertTrue(version.quality.hard_passed)
                self.assertEqual(context.tool_calls, [])
                self.assertEqual((context.opens, context.closes), (1, 1))
                self.assertNotIn('tool_execution', self.purposes(pipeline, incident.id))

    def test_request_builder_failure_keeps_baseline_generation(self):
        generator = ToolLoopGenerator()
        def unavailable(*args):
            raise ValueError('incompatible tool request')
        generator.build_tool_request = unavailable
        pipeline = self.pipeline(generator, FakeCodeContext())
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(generator.requests, [])
        self.assertIn('tool_exploration_summary', self.purposes(pipeline, incident.id))

    def test_disabled_tools_preserve_existing_workflow(self):
        generator = ToolLoopGenerator([AssertionError("must not request tools")])
        context = FakeCodeContext()
        pipeline = self.pipeline(generator, context, tools_enabled=False)
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(generator.requests, [])
        self.assertEqual(context.tool_calls, [])
        self.assertNotIn("tool_exploration", self.purposes(pipeline, incident.id))

    def test_custom_generator_without_tool_protocol_remains_compatible(self):
        pipeline = self.pipeline(FakeGenerator(), FakeCodeContext())
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertNotIn("tool_exploration", self.purposes(pipeline, incident.id))

    def test_history_search_keeps_server_scope_and_never_creates_current_evidence(self):
        store = ScopedHistoryStore()
        generator = ToolLoopGenerator([
            [function_call("history", "search_historical_cases", {"query": "registration recovery", "limit": 2})],
            [],
        ])
        pipeline = self.pipeline(generator, FakeCodeContext(), store)
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)
        result = returned_tools(generator.requests[1])["history"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["historical_cases"][0]["knowledge_id"], "knowledge_old_case")
        self.assertTrue(all((repo, kind) == ("team/sdk", "bug_fix") for _, repo, kind, _ in store.queries))
        self.assertFalse(any(e.id == "knowledge_old_case" or e.source_type == EvidenceSource.HISTORICAL
                             for e in store.list_evidence(incident.id)))
        self.assertNotIn("knowledge_old_case", version.evidence_ids)

    def test_historical_reference_cannot_pass_as_current_evidence_citation(self):
        generator = ToolLoopGenerator([
            [function_call("history", "search_historical_cases", {"query": "registration recovery", "limit": 2})],
            [],
        ], cite_history=True)
        pipeline = self.pipeline(generator, FakeCodeContext(), ScopedHistoryStore())
        incident = self.incident(pipeline)
        with self.assertRaisesRegex(PipelineError, "Unknown evidence ID"):
            pipeline.run(incident.id)
        self.assertEqual(len(generator.requests), 2)
        self.assertEqual(pipeline.store.list_versions(incident.id), [])

    def test_all_excess_call_ids_receive_budget_result_and_rounds_are_bounded(self):
        calls = [function_call(f"chat_{i}", "search_feishu_context", {"query": "上传参数校验缺失", "limit": i})
                 for i in range(1, 6)]
        generator = ToolLoopGenerator([calls, [], [function_call("unused", "missing", {})]])
        pipeline = self.pipeline(generator, FakeCodeContext())
        incident = self.incident(pipeline)
        pipeline.run(incident.id)
        results = returned_tools(generator.requests[1])
        self.assertEqual(set(results), {f"chat_{i}" for i in range(1, 6)})
        self.assertEqual(results["chat_5"]["status"], "budget_exhausted")
        self.assertEqual(len(generator.requests), 2)

    def test_validation_retry_does_not_repeat_exploration(self):
        generator = ToolLoopGenerator([
            [function_call("code", "get_related_code", {"symbol": "Client.watcher", "direction": "both", "limit": 3})],
            [],
        ], cite_code=True, invalid_first=True)
        context = FakeCodeContext()
        pipeline = self.pipeline(generator, context)
        incident = self.incident(pipeline)
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(len(generator.requests), 2)
        self.assertEqual(len(generator.final_inputs), 2)
        self.assertEqual(context.opens, 1)
        self.assertEqual(len(context.tool_calls), 1)
        self.assertTrue(generator.final_inputs[1][0].metadata["generation_validation_errors"])

    def test_regeneration_reuses_persisted_evidence_without_reopening_tools(self):
        generator = ToolLoopGenerator([
            [function_call("code", "get_related_code", {"symbol": "Client.watcher", "direction": "both", "limit": 3})],
            [],
        ], cite_code=True)
        context = FakeCodeContext()
        pipeline = self.pipeline(generator, context)
        incident = self.incident(pipeline)
        pipeline.run(incident.id)
        pipeline.reject(incident.id, "reviewer", "verify persisted context")
        version = pipeline.regenerate(incident.id)
        self.assertEqual(version.version, 2)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(len(generator.requests), 2)
        self.assertEqual((context.opens, context.closes), (1, 1))
        self.assertEqual(len(context.tool_calls), 1)
        self.assertEqual(version.document.fix_solution.citations[0].evidence_id, f"ev_tool_restore_{incident.id}")
