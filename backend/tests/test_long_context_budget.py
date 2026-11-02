"""Long material passes the full evidence path without bypassing source guards."""
import json
import queue

from starlette.testclient import TestClient

from repo_maintainer.investigation_runner import InvestigationSession, investigation_input
from repo_maintainer.retrospective_mcp import InvestigationGateway
from repo_maintainer.retrospective_models import Evidence, EvidenceSource
from repo_maintainer.retrospective_pipeline import ContextSelector
from repo_maintainer.retrospective_tools import ToolExecutor
from test_deepseek_runner import context, settings


def test_long_material_reaches_explorer_tool_and_writer_without_mutating_originals():
    ctx = context()
    raw = [Evidence(id=f'ev_long_{i}', incident_id=ctx.incident.id, source_type=EvidenceSource.FEISHU_DOC,
                    content=f'phase{i} ' + 'synthetic context pending verification. ' * 1000,
                    location=f'synthetic/doc/{i}') for i in range(5)]
    original = [e.content for e in raw]
    ctx.raw_evidence = raw
    ctx.selected = ContextSelector().select(ctx.incident, raw).evidence
    view = investigation_input(ctx, budget_chars=160_000, item_chars=24_000, max_items=64)
    assert sum(len(e['content']) for e in view['evidence']) > 8_000
    assert sum(len(e.content) for e in ctx.selected) > 24_000
    executor = ToolExecutor(ctx.incident, raw, output_budget_chars=600_000, item_chars=12_000, max_calls=80)
    for i in range(5):
        result = executor.execute('search_feishu_context', {'query': f'phase{i}', 'limit': 1})
        assert len(result.evidence[0]['content']) == 12_000
        assert result.evidence[0]['material_role'] == 'document_statement'
        assert result.evidence[0]['impact_coverage'] == 'unknown'
    assert executor.used_chars > 12_000
    assert [e.content for e in raw] == original


def test_large_request_passes_proxy_and_is_recorded_before_forwarding(tmp_path, monkeypatch):
    """Exercise both body guards with >256 KB, actual async proxy and persistence."""
    gateway = InvestigationGateway(InvestigationSession(context()), settings(tmp_path), queue.Queue())
    saved = []

    async def submit(kind, payload):
        assert kind == 'model_request'
        saved.append(payload)
        return True

    class Response:
        status_code = 200
        headers = {'content-type': 'text/event-stream'}

        async def aiter_raw(self):
            yield b'data: [DONE]\n\n'

        async def aclose(self): pass

    class Client:
        def __init__(self, **kwargs):
            assert 15 < kwargs['timeout'] <= 120

        def build_request(self, method, url, **kwargs):
            assert len(json.dumps(kwargs['json']).encode()) > 256_000
            assert saved[-1]['body'] == kwargs['json']
            return kwargs['json']

        async def send(self, request, **kwargs): return Response()
        async def aclose(self): pass

    monkeypatch.setattr(gateway, 'submit', submit)
    monkeypatch.setattr('httpx.AsyncClient', Client)
    names = ['get_related_code', 'search_feishu_context', 'search_historical_cases']
    body = {'model': 'model', 'messages': [{'role': 'user', 'content': 'synthetic ' * 40_000}],
            'tools': [{'function': {'name': 'mcp__retrospective__' + n}} for n in names]}
    with TestClient(gateway._app(), base_url='http://127.0.0.1') as client:
        result = client.post('/model/chat/completions', json=body,
                             headers={'Authorization': 'Bearer ' + gateway.token})
    assert result.status_code == 200
    assert len(saved) == 1 and gateway.model_requests == 1
