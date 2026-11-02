"""Opt-in real pinned Harness/MCP round trip against a local model fixture."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from repo_maintainer.deepseek_runner import DeepSeekInvestigationRunner
from repo_maintainer.retrospective_harness_state import HarnessSettings
from repo_maintainer.retrospective_models import IncidentCreate
from repo_maintainer.retrospective_pipeline import RetrospectivePipeline
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore, PostgresRetrospectiveStore
from test_retrospective_pipeline import FakeCollaboration, FakeEmbedder, FakeGit
from test_retrospective_tool_loop import FakeCodeContext, ToolLoopGenerator
from test_repository_search_strategy import repository


@pytest.mark.skipif(os.getenv("RUN_HARNESS_INTEGRATION") != "1", reason="requires pinned Harness runtime and loopback")
@pytest.mark.parametrize("behavior", ["normal", "round_limit", "provider_failure", "scope_injection"])
@pytest.mark.parametrize("store_kind", ["memory", "postgres"])
def test_real_runtime_mcp_evidence_round_trip(tmp_path, behavior, store_kind):
    from deepseek_harness import DeepSeekHarness
    if store_kind == "postgres" and not os.getenv("TEST_CONTEXT_DATABASE_URL"):
        pytest.skip("isolated PostgreSQL not configured")
    requests, runtimes = [], []

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            assert self.headers["Authorization"] == "Bearer fixture-key"
            snapshots = [r.payload["request"]["body"] for r in pipeline.store.list_generation_contexts(incident.id)
                         if r.payload["purpose"] == "tool_exploration"]
            assert snapshots[-1] == body  # persisted before the upstream dispatch
            if behavior == "provider_failure":
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            if len(requests) == 1 or behavior == "round_limit":
                name = next(t["function"]["name"] for t in body["tools"] if t["function"]["name"].endswith("get_related_code"))
                arguments = {"symbol": "Client.watcher", "direction": "both", "limit": 4 - len(requests)}
                if behavior == "scope_injection":
                    arguments["repo_id"] = "unauthorized/repo"
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "fixture-code", "type": "function",
                             "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "Evidence collected."}, "stop"
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}]
            for chunk in chunks:
                chunk.update(id="fixture-response", object="chat.completion.chunk", created=1, model="fixture-model")
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def factory(**kwargs):
        runtime = DeepSeekHarness(**kwargs)
        runtimes.append(runtime)
        return runtime

    context = FakeCodeContext()
    store = (PostgresRetrospectiveStore(os.environ["TEST_CONTEXT_DATABASE_URL"])
             if store_kind == "postgres" else InMemoryRetrospectiveStore())
    if store_kind == "postgres":
        store.initialize()

    class FixtureEmbedder(FakeEmbedder):
        def embed_texts(self, texts):
            return [vector + [0.0] * (1024 - len(vector)) for vector in super().embed_texts(texts)]

    pipeline = RetrospectivePipeline(
        store=store, git=FakeGit(), collaboration=FakeCollaboration(),
        generator=ToolLoopGenerator(cite_code=True), embedder=FixtureEmbedder(), code_context=context,
        investigation_runner=DeepSeekInvestigationRunner(HarnessSettings(
            model="fixture-model", api_key="fixture-key", base_url=f"http://127.0.0.1:{server.server_port}/v1",
            root=tmp_path, timeout_seconds=45, max_rounds=2), sdk_factory=factory),
    )
    incident = pipeline.create_incident(IncidentCreate(title="registration recovery", repo_id="team/sdk",
        repo_name="team/sdk", commit_sha="a" * 40, incident_type="bug_fix"))
    try:
        version = pipeline.run(incident.id)
        records = pipeline.store.list_generation_contexts(incident.id)
        summary = next(r.payload["request"] for r in records if r.payload["purpose"] == "tool_exploration_summary")
        diagnostics = runtimes[0].client._runtime_diagnostics() if runtimes else "No runtime"
        expected = {"normal": "no_tool_calls", "round_limit": "round_limit", "provider_failure": "model_error",
                    "scope_injection": "no_tool_calls"}[behavior]
        if summary["stop_reason"] != expected:
            pytest.fail(str(summary) + "\n" + diagnostics)
        assert summary["rounds"] == (1 if behavior == "provider_failure" else 2)
        assert len(requests) == summary["rounds"]
        assert len(context.tool_calls) == {"normal": 1, "round_limit": 2, "provider_failure": 0, "scope_injection": 0}[behavior]
        assert context.opens == context.closes == 1
        assert any(eid.startswith("ev_tool_restore_") for eid in version.evidence_ids) == (behavior in {"normal", "round_limit"})
        assert version.quality.hard_passed
        assert version.prompt_version == "v6-human-review-feedback"
        assert any(r.payload["purpose"] == "harness_call_mapping" for r in records) == (behavior in {"normal", "round_limit"})
        native_ids = {
            r.payload["request"]["payload"]["event"]["data"]["callId"] for r in records
            if r.payload["purpose"] == "harness_event"
            and r.payload["request"]["payload"].get("event", {}).get("type") == "tool/call"
        }
        for record in records:
            if record.payload["purpose"] == "harness_call_mapping":
                assert record.payload["request"]["harness_call_id"] in native_ids
        assert len([r for r in records if r.payload["purpose"] == "tool_exploration"]) == len(requests)
        assert all(len(r["tools"]) == 6 for r in requests)
        assert "fixture-key" not in "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*/retrospective.patch.json"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(os.getenv('RUN_HARNESS_INTEGRATION') != '1', reason='requires pinned Harness runtime and loopback')
@pytest.mark.parametrize('store_kind', ['memory', 'postgres'])
def test_real_runtime_search_read_related_and_candidate_review(tmp_path, repository, store_kind):
    """Real SDK drives new tools; the local model fixture makes no cloud calls."""
    from repo_maintainer.retrospective_models import Citation, PotentialImpact
    if store_kind == 'postgres' and not os.getenv('TEST_CONTEXT_DATABASE_URL'):
        pytest.skip('isolated PostgreSQL not configured')
    _, code_context, _ = repository
    requests, failures = [], []

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                tools = {t['function']['name'].split('__')[-1]: t['function']['name'] for t in body['tools']}
                assert len(tools) == 6
                number = len(requests)
                calls = []
                if number == 1:
                    calls = [('find_repository_files', {'pattern': '.insight/*', 'limit': 10}),
                             ('search_repository_code', {'query': 'recover', 'file_pattern': 'services/*', 'case_sensitive': True, 'limit': 10})]
                elif number == 2:
                    tool_messages = [m for m in body['messages'] if m['role'] == 'tool']
                    assert any('services/payment.ts' in str(m['content']) for m in tool_messages)
                    assert any('navigation_only' in str(m['content']) for m in tool_messages)
                    calls = [('read_repository_file', {'path': 'services/payment.ts', 'start_line': 1, 'end_line': 2}),
                             ('read_repository_file', {'path': 'sdk/client.py', 'start_line': 1, 'end_line': 2})]
                elif number == 3:
                    assert 'static_candidate' in json.dumps(body['messages'])
                    calls = [('get_related_code', {'symbol': 'recover', 'direction': 'both', 'limit': 6})]
                delta = {'role': 'assistant'}
                if calls:
                    delta['tool_calls'] = [{'index': i, 'id': f'search-{number}-{i}', 'type': 'function',
                        'function': {'name': tools[name], 'arguments': json.dumps(arguments)}}
                        for i, (name, arguments) in enumerate(calls)]
                else:
                    delta['content'] = 'Static candidates need runtime verification.'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                chunks = [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                          {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}]
                for chunk in chunks:
                    chunk.update(id='search-fixture', object='chat.completion.chunk', created=1, model='fixture-model')
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except Exception as exc:
                failures.append(repr(exc))
                self.send_error(500)

    class ScenarioWriter(ToolLoopGenerator):
        def generate(self, incident, evidence, history):
            document, metadata = super().generate(incident, evidence, history)
            code = next(e for e in evidence if e.location.startswith('services/payment.ts#'))
            document.impact.scope_basis = 'observed'
            document.impact.coverage = 'partial'
            document.impact.potential_impacts = [PotentialImpact(
                content='若支付服务部署相同 SDK，恢复流程可能受影响，尚无运行证据。',
                citations=[Citation(evidence_id=code.id, quote='export const pay = () => recover();')],
                verification_needed='核对支付服务部署版本、事故时间窗及恢复日志。')]
            return document, metadata

    class FixtureEmbedder(FakeEmbedder):
        def embed_texts(self, texts):
            return [v + [0.0] * (1024 - len(v)) for v in super().embed_texts(texts)]

    store = (PostgresRetrospectiveStore(os.environ['TEST_CONTEXT_DATABASE_URL'])
             if store_kind == 'postgres' else InMemoryRetrospectiveStore())
    if store_kind == 'postgres':
        store.initialize()
    server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pipeline = RetrospectivePipeline(store=store, git=FakeGit(), collaboration=FakeCollaboration(),
            generator=ScenarioWriter(), embedder=FixtureEmbedder(), code_context=code_context,
            investigation_runner=DeepSeekInvestigationRunner(HarnessSettings(
                model='fixture-model', api_key='fixture-key', base_url=f'http://127.0.0.1:{server.server_port}/v1',
                root=tmp_path / 'harness', timeout_seconds=45, max_rounds=4)))
        incident = pipeline.create_incident(IncidentCreate(title='image service RST_STREAM', repo_id='team/sdk',
            repo_name='team/sdk', commit_sha='a' * 40))
        version = pipeline.run(incident.id)
        records = store.list_generation_contexts(incident.id)
        summary = next(r.payload['request'] for r in records if r.payload['purpose'] == 'tool_exploration_summary')
        assert failures == []
        assert len(requests) == 4 and summary['calls_used'] == 5
        assert summary['stop_reason'] == 'no_tool_calls'
        assert version.quality.hard_passed and version.prompt_version == 'v6-human-review-feedback'
        restored = store.latest_version(incident.id)
        assert restored.document.impact.coverage == 'partial'
        candidate = restored.document.impact.potential_impacts[0]
        assert candidate.status == 'unverified'
        assert candidate.citations[0].evidence_id in version.evidence_ids
        assert pipeline.store.get_incident(incident.id).status.value == 'pending_review'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
