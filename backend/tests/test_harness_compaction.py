"""Native compression configuration, provenance checks and bounded model proxy."""

import copy
import json
import queue

import pytest
from starlette.testclient import TestClient

from repo_maintainer.investigation_runner import InvestigationSession
from repo_maintainer.retrospective_compaction import (
    CompactionContractError, LEDGER_TAG, POLICY, prepare_compaction, reported_summary_usage, summary_from_response, validate_summary,
)
from repo_maintainer.retrospective_harness_state import runtime_patch
from repo_maintainer.retrospective_harness_trace import HarnessTrace
from repo_maintainer.retrospective_mcp import InvestigationGateway
from repo_maintainer.retrospective_models import Evidence, EvidenceSource
from repo_maintainer.retrospective_tools import ToolExecutor
from test_deepseek_runner import context, settings


def materials():
    return [Evidence(id='ev_static', incident_id='incident', source_type=EvidenceSource.REPOSITORY_CODE,
                     content='payment.recover()', location='sdk.py#L2', repo_id='repo', commit_sha='a' * 40,
                     occurred_at='2026-11-01T00:00:00+00:00'),
            Evidence(id='ev_chat', incident_id='incident', source_type=EvidenceSource.FEISHU_MESSAGE,
                     content='only image service reported', location='chat/message-1', occurred_at='2026-11-01T00:00:00+00:00')]


def envelope():
    return {'messages': [{'role': 'user', 'content': 'ev_chat ev_static reported image; payment unknown'},
                         {'role': 'user', 'content': 'Native compaction instruction'}]}


def checkpoint(anchors):
    return f'## Critical Context\nReported image; static payment candidate remains unverified.\n<{LEDGER_TAG}>\n{json.dumps(anchors)}\n</{LEDGER_TAG}>'


def test_contract_adds_provenance_without_changing_original_request_or_evidence():
    body, evidence = envelope(), materials()
    original = copy.deepcopy(body)
    changed, anchors = prepare_compaction(body, evidence)
    assert body == original
    assert changed['tool_choice'] == 'none' and POLICY in changed['messages'][-1]['content']
    assert anchors[1]['material_role'] == 'static_candidate' and anchors[1]['impact_status'] == 'unverified'
    assert anchors[1]['commit_sha'] == 'a' * 40
    validate_summary(checkpoint(anchors), anchors)
    assert evidence[0].content == 'payment.recover()'


@pytest.mark.parametrize('change', ['omit', 'role', 'sha', 'time', 'promote', 'duplicate', 'malformed'])
def test_summary_cannot_drop_or_reclassify_protected_sources(change):
    _, anchors = prepare_compaction(envelope(), materials())
    edited = copy.deepcopy(anchors)
    if change == 'omit': edited.pop()
    if change == 'role': edited[1]['material_role'] = 'log_observation'
    if change == 'sha': edited[1]['commit_sha'] = 'b' * 40
    if change == 'time': edited[1]['recorded_at'] = '2020-01-01T00:00:00Z'
    if change == 'promote': edited[1]['impact_status'] = 'confirmed'
    text = checkpoint(edited)
    if change == 'duplicate': text += text
    if change == 'malformed': text = f'<{LEDGER_TAG}>not json</{LEDGER_TAG}>'
    with pytest.raises(CompactionContractError):
        validate_summary(text, anchors)


def test_only_sources_in_selected_prefix_are_protected_and_prior_checkpoint_survives():
    body = envelope()
    body['messages'][0]['content'] = 'ev_static_more is a different source ID'
    _, anchors = prepare_compaction(body, materials())
    assert anchors == []
    _, anchors = prepare_compaction(envelope(), materials())
    body['messages'][0]['content'] = checkpoint(anchors)
    _, repeated = prepare_compaction(body, materials())
    assert repeated == anchors


@pytest.mark.parametrize('reason', ['length', 'tool_calls', None])
def test_truncated_or_incomplete_summary_stream_is_rejected(reason):
    chunk = {'choices': [{'index': 0, 'delta': {'content': 'summary'}, 'finish_reason': reason}]}
    raw = ('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode()
    with pytest.raises(CompactionContractError):
        summary_from_response(raw, 'text/event-stream')


def test_valid_sse_and_json_response():
    text = 'Unicode 中文 summary'
    chunks = [{'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]},
              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
    raw = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks).encode()
    assert summary_from_response(raw, 'text/event-stream') == text
    raw = json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]}).encode()
    assert summary_from_response(raw, 'application/json') == text


@pytest.mark.parametrize('raw,content_type', [(b'null', 'application/json'),
    (b'data: null\n\n', 'text/event-stream'),
    (b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":"stop"}]}\n\n'
     b'data: {"choices":[{"index":0,"delta":{"content":"late ledger"},"finish_reason":null}]}\n\n', 'text/event-stream')])
def test_malformed_or_post_finish_data_cannot_supply_a_false_ledger(raw, content_type):
    with pytest.raises(CompactionContractError):
        summary_from_response(raw, content_type)


@pytest.mark.parametrize('values', [{'compaction_max_requests': 0}, {'compaction_max_requests': 33},
    {'compaction_max_tokens': 255}, {'compaction_threshold': 0.1}, {'compaction_threshold': float('nan')},
    {'compaction_headroom': 1_000_000}, {'api': 'openai-responses'}])
def test_invalid_compaction_configuration_fails_before_startup(tmp_path, values):
    with pytest.raises(ValueError):
        settings(tmp_path, **values)


def test_disable_is_an_explicit_rollback_and_keeps_other_protocol_available(tmp_path):
    config = settings(tmp_path, compaction_enabled=False, api='openai-responses')
    row = next(r for r in runtime_patch(config, 'http://127.0.0.1:1', 'token') if r['id'] == 'compaction-basic')
    assert row['disabled'] is True


def test_successful_compaction_can_rehydrate_cached_windows_without_refetch_or_extra_execution():
    c = context()
    evidence = materials()[1].model_copy(update={'incident_id': c.incident.id})
    executor = ToolExecutor(c.incident, [evidence], output_budget_chars=1000)
    args = {'query': 'image', 'limit': 1}
    first = executor.execute('search_feishu_context', args)
    assert first.evidence
    assert not executor.execute('search_feishu_context', args).evidence
    executor.on_compaction()
    again = executor.execute('search_feishu_context', args)
    assert again.cache_hit and again.evidence[0]['content'] == first.evidence[0]['content']
    assert executor.calls_used == 1 and executor.used_chars == len(evidence.content) * 2
    assert executor.added_evidence == []
    assert not executor.execute('search_feishu_context', args).evidence


def test_compaction_does_not_reset_delivery_budget():
    c = context()
    evidence = materials()[1].model_copy(update={'incident_id': c.incident.id})
    executor = ToolExecutor(c.incident, [evidence], output_budget_chars=len(evidence.content))
    args = {'query': 'image', 'limit': 1}
    executor.execute('search_feishu_context', args)
    executor.on_compaction()
    assert executor.execute('search_feishu_context', args).status == 'budget_exhausted'


@pytest.mark.parametrize('failed', [False, True])
def test_only_successful_native_checkpoint_reopens_cache_delivery(failed):
    c = context()
    evidence = materials()[1].model_copy(update={'incident_id': c.incident.id})
    session = InvestigationSession(c)
    session.executor = ToolExecutor(c.incident, [evidence])
    args = {'query': 'image', 'limit': 1}
    session.executor.execute('search_feishu_context', args)
    trace = HarnessTrace(session)
    trace.notification('session.event', {'event': {'type': 'compaction/end', 'data': {'error': 'failed'} if failed else {}}})
    assert bool(session.executor.execute('search_feishu_context', args).evidence) is not failed
    assert len(trace.compaction_failures) == int(failed)


@pytest.mark.parametrize('streaming', [False, True])
def test_rejected_native_summary_keeps_reported_usage_and_cached_input(streaming):
    value={'choices': [],'usage': {'prompt_tokens':120,'completion_tokens':30,'total_tokens':150,
                                 'prompt_tokens_details':{'cached_tokens':80}}}
    raw=json.dumps(value).encode()
    if streaming: raw=b'data: '+raw+b'\n\ndata: [DONE]\n\n'
    usage=reported_summary_usage(raw,'text/event-stream' if streaming else 'application/json')
    assert usage=={'purpose':'compaction','inputTokens':40,'outputTokens':30,'cacheReadTokens':80,'totalTokens':150}


@pytest.mark.parametrize('case,expected', [('disabled', 404), ('limit', 429), ('model', 400), ('output', 400), ('tool', 400)])
def test_summary_endpoint_is_scoped_and_separately_limited(tmp_path, case, expected):
    config = settings(tmp_path, compaction_enabled=case != 'disabled')
    gateway = InvestigationGateway(InvestigationSession(context()), config, queue.Queue())
    body = {'model': config.model, 'messages': envelope()['messages'], 'max_tokens': config.compaction_max_tokens}
    if case == 'limit': gateway.compaction_requests = config.compaction_max_requests
    if case == 'model': body['model'] = 'foreign-model'
    if case == 'output': body['max_tokens'] += 1
    if case == 'tool': body['tools'] = [{'function': {'name': 'bash'}}]
    with TestClient(gateway._app(), base_url='http://127.0.0.1') as client:
        result = client.post('/compaction-model/chat/completions', json=body,
                             headers={'Authorization': 'Bearer ' + gateway.token})
    assert result.status_code == expected
    assert gateway.model_requests == 0 and gateway.events.empty()
