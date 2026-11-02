"""Real pinned DSH automatic pressure compaction against a local model fixture."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from repo_maintainer.deepseek_runner import DeepSeekInvestigationRunner
from repo_maintainer.investigation_runner import InvestigationContext
from repo_maintainer.retrospective_compaction import POLICY
from repo_maintainer.retrospective_harness_state import HarnessSettings
from repo_maintainer.retrospective_models import Evidence, EvidenceSource, IncidentRecord
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore, PostgresRetrospectiveStore
from test_harness_compaction import checkpoint


@pytest.mark.skipif(os.getenv('RUN_HARNESS_INTEGRATION') != '1', reason='requires pinned runtime and loopback')
@pytest.mark.parametrize('behavior', ['ok', 'invalid', 'provider_failure', 'disabled', 'limit'])
@pytest.mark.parametrize('store_kind', ['memory', 'postgres'])
def test_native_compaction_preserves_raw_evidence_and_bounds_requests(tmp_path, behavior, store_kind):
    if store_kind == 'postgres' and not os.getenv('TEST_CONTEXT_DATABASE_URL'):
        pytest.skip('isolated PostgreSQL not configured')
    store = (PostgresRetrospectiveStore(os.environ['TEST_CONTEXT_DATABASE_URL'])
             if store_kind == 'postgres' else InMemoryRetrospectiveStore())
    if store_kind == 'postgres': store.initialize()
    incident = store.create_incident(IncidentRecord(title='image report; payment impact unknown', repo_id='team/sdk', commit_sha='a' * 40))
    raw = [Evidence(id=f'ev_long_{i}_{incident.id}', incident_id=incident.id, source_type=EvidenceSource.FEISHU_MESSAGE,
        location=f'chat/message-{i}', content='RST_STREAM reported image only. ' + 'request failed registered service recovery pending verification. ' * 45)
        for i in range(5)]
    raw.append(Evidence(id=f'ev_static_{incident.id}', incident_id=incident.id, source_type=EvidenceSource.REPOSITORY_CODE,
        content='payment.recover()', location='sdk.py#L2', repo_id=incident.repo_id, commit_sha=incident.commit_sha))
    store.add_evidence(raw)
    requests, summaries, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                compact = body.get('tool_choice') == 'none'
                target = summaries if compact else requests
                target.append(body)
                purpose = 'harness_compaction_request' if compact else 'tool_exploration'
                snapshots = [r.payload['request']['body'] for r in store.list_generation_contexts(incident.id)
                             if r.payload['purpose'] == purpose]
                assert snapshots[-1] == body
                assert self.headers['Authorization'] == 'Bearer fixture-key'
                if compact and behavior == 'provider_failure':
                    self.send_error(503)
                    return
                delta = {'role': 'assistant'}
                if compact:
                    last = body['messages'][-1]['content']
                    instruction = last if isinstance(last, str) else last[-1]['text']
                    anchors = json.loads(instruction.split(POLICY, 1)[1])
                    delta['content'] = checkpoint(anchors) if behavior not in {'invalid', 'limit'} else 'Unverified summary without source ledger.'
                    finish = 'stop'
                elif len(requests) <= 2:
                    name = next(t['function']['name'] for t in body['tools'] if t['function']['name'].endswith('search_feishu_context'))
                    arguments = {'query': 'RST_STREAM' if len(requests) == 1 else 'no-other-observations-found', 'limit': 5}
                    delta['tool_calls'] = [{'index': 0, 'id': f'tool-{len(requests)}', 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(arguments)}}]
                    finish = 'tool_calls'
                else:
                    delta['content'], finish = 'Reported image; payment remains unverified.', 'stop'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                for value in [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                               'usage': {'prompt_tokens': 500, 'completion_tokens': 50, 'total_tokens': 550}}]:
                    value.update(id='compact-fixture', model='fixture-model', object='chat.completion.chunk', created=1)
                    self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except Exception as exc:
                errors.append(repr(exc))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = HarnessSettings(model='fixture-model', api_key='fixture-key',
            base_url=f'http://127.0.0.1:{server.server_port}/v1', root=tmp_path, timeout_seconds=45,
            context_tokens=8192, compaction_threshold=0.65, compaction_headroom=512,
            max_output_tokens=700, compaction_max_tokens=2048,
            initial_context_chars=8000, initial_item_chars=1000, tool_item_chars=2400,
            compaction_enabled=behavior != 'disabled', compaction_max_requests=1 if behavior == 'limit' else 2)
        context = InvestigationContext(incident, raw, raw, [], store, object(), lambda *_: [])
        *_, summary = DeepSeekInvestigationRunner(config).run(context)
        assert errors == [], errors
        assert len(requests) == 3, summary
        assert summary['rounds'] == 3 and summary['total_model_requests'] == len(requests) + len(summaries)
        records = store.list_generation_contexts(incident.id)
        outcomes = [r.payload['request'] for r in records if r.payload['purpose'] == 'harness_compaction_result']
        billed_summaries=[u for u in summary['model_usage'] if u.get('purpose')=='compaction']
        assert len(billed_summaries)==(0 if behavior in {'provider_failure','disabled'} else len(summaries))
        assert all(u['totalTokens']==550 for u in billed_summaries)
        if behavior in {'invalid','provider_failure','limit'}:
            assert summary['compaction_native_errors']>0
        if behavior == 'disabled':
            assert not summaries and not summary['compaction_events']
        else:
            assert 1 <= len(summaries) <= config.compaction_max_requests, summary
            assert 'compaction/start' in summary['compaction_events']
            if behavior == 'ok':
                assert 'compaction/summary' in summary['compaction_events'], summary
                assert any(o['accepted'] for o in outcomes)
                assert any('<compacted-summary>' in json.dumps(r['messages']) for r in requests[1:])
            else:
                assert summary['compaction_errors'] > 0
                assert not any(o['accepted'] for o in outcomes)
                assert 'compaction/summary' not in summary['compaction_events']
                assert not any('<compacted-summary>' in json.dumps(r['messages']) for r in requests)
                if behavior == 'limit':
                    assert len(summaries) == 1 and summary['stop_reason'] == 'compaction_limit', summary
        restored = {e.id: e for e in store.list_evidence(incident.id)}
        assert all(restored[e.id].content == e.content for e in raw)
        assert len(restored) == len(raw)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
