"""Real-model failures must use bounded schema feedback without accepting drafts."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from repo_maintainer.retrospective_connectors import OpenAIReviewGenerator
from repo_maintainer.retrospective_models import IncidentCreate, IncidentStatus
from repo_maintainer.retrospective_pipeline import PipelineError
from repo_maintainer.retrospective_tools import ToolExecutor
from test_retrospective_pipeline import FakeCollaboration, FakeGenerator, FakeGit, make_pipeline


def setup_case():
    client=Mock()
    with patch('openai.OpenAI',return_value=client):
        generator=OpenAIReviewGenerator(api_key='fixture-key',model='fixture-model')
    pipeline=make_pipeline(generator=generator)
    pipeline.tools_enabled=False
    incident=pipeline.create_incident(IncidentCreate(title='upload'))
    evidence=FakeGit().collect(incident)+FakeCollaboration().collect(incident)
    good,_=FakeGenerator().generate(incident,evidence,[])
    bad=good.model_dump(mode='json')
    bad['impact'].pop('citations')
    bad['impact']['scope']='SYNTHETIC_PRIVATE_INVALID_DRAFT'
    return client,pipeline,incident,good.model_dump(mode='json'),bad


def response(document):
    calls=[] if document is None else [SimpleNamespace(type='function_call',name='submit_retrospective',arguments=json.dumps(document))]
    return SimpleNamespace(output=calls,usage=SimpleNamespace(input_tokens=100,output_tokens=20),model='fixture-model')


def test_missing_impact_citations_are_fed_back_and_only_valid_draft_is_saved():
    client,pipeline,incident,good,bad=setup_case()
    client.responses.create.side_effect=[response(bad),response(good)]
    version=pipeline.run(incident.id)
    assert version.quality.hard_passed and client.responses.create.call_count==2
    assert pipeline.store.get_incident(incident.id).status==IncidentStatus.PENDING_REVIEW
    assert len(pipeline.store.list_versions(incident.id))==1
    records=pipeline.store.list_generation_contexts(incident.id)
    requests=[r.payload['request'] for r in records if r.payload['purpose']=='retrospective']
    feedback=json.loads(requests[1]['input'])['incident']['previous_validation_errors']
    assert any('impact.citations' in error for error in feedback)
    failures=[r.payload['request'] for r in records if r.payload['purpose']=='generation_contract_failure']
    assert len(failures)==1 and failures[0]['usage']['input_tokens']==100
    assert 'SYNTHETIC_PRIVATE_INVALID_DRAFT' not in json.dumps(failures)


@pytest.mark.parametrize('kind',['missing_field','missing_function'])
def test_structural_failures_stop_at_existing_attempt_budget_and_never_publish(kind):
    client,pipeline,incident,_good,bad=setup_case()
    client.responses.create.side_effect=[response(bad if kind=='missing_field' else None)]*2
    with pytest.raises(PipelineError,match='invalid retrospective payload'):
        pipeline.run(incident.id)
    assert client.responses.create.call_count==2
    assert pipeline.store.get_incident(incident.id).status==IncidentStatus.FAILED
    assert not pipeline.store.list_versions(incident.id)
    assert pipeline.store.list_evidence(incident.id)
    assert pipeline.collaboration.publish_calls==0


def test_empty_chat_source_reports_scope_and_disallows_query_variation_retry():
    _client,_pipeline,incident,_good,_bad=setup_case()
    result=ToolExecutor(incident,[]).execute('search_feishu_context',{'query':'synthetic','limit':3})
    assert result.status=='empty'
    assert result.search_scope['eligible_materials']==0
    assert result.search_scope['external_fetch'] is False
    assert result.search_scope['retry_with_new_query'] is False
    assert result.search_scope['impact_coverage']=='unknown'
