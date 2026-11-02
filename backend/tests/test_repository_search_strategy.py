"""Search scope, bounded discovery and incomplete impact evidence contracts."""

import json
import time
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from repo_maintainer.investigation_runner import InvestigationContext, investigation_input
from repo_maintainer.retrospective_code_context import RepositoryCodeContext
from repo_maintainer.retrospective_connectors import OpenAIReviewGenerator, _document_markdown
from repo_maintainer.retrospective_models import (
    Citation, Evidence, EvidenceSource, ImpactSection, IncidentRecord, PotentialImpact,
)
from repo_maintainer.retrospective_pipeline import QualityValidator, _document_citations, _knowledge_chunks
from repo_maintainer.retrospective_repository_search import SnapshotSearch
from repo_maintainer.retrospective_tools import ToolExecutor, ToolPersistenceError
from test_retrospective_pipeline import FakeGenerator


@pytest.fixture
def repository(tmp_path):
    paths = {
        'sdk/client.py': 'def recover():\n    return register()\n\ndef register():\n    return "RST_STREAM"\n',
        'services/image.py': 'from sdk.client import recover\n\ndef run():\n    return recover()\n',
        'services/payment.ts': 'import {recover} from "../sdk/client";\nexport const pay = () => recover();\n',
        'docs/recovery.md': 'recovery requires registration\nLiteral foo.* marker\n',
        '.insight/context.json': json.dumps({'services': [
            {'id': 'image', 'paths': ['services/image.py'], 'dependencies': ['sdk']},
            {'id': 'payment', 'paths': ['services/payment.ts'], 'dependencies': ['sdk']},
            {'id': 'sdk', 'paths': ['sdk']},
        ]}),
    }
    for path, text in paths.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    incident = IncidentRecord(id='incident-search', title='图像服务掉线', repo_id='team/sdk',
                              repo_name='team/sdk', commit_sha='a' * 40)

    class Source:
        @contextmanager
        def open(self, _incident):
            yield tmp_path

    return incident, RepositoryCodeContext(Source()), tmp_path


def executor_for(incident, session, **kwargs):
    return ToolExecutor(incident, [], code_session=session, repository_search_enabled=True,
                        max_calls=12, **kwargs)


def test_search_finds_unreported_consumer_and_persists_only_selected_source(repository):
    incident, context, _ = repository
    saved = []
    with context.open_session(incident) as session:
        executor = executor_for(incident, session, persist_evidence=lambda items: saved.extend(items))
        result = executor.execute('search_repository_code', {
            'query': 'recover', 'file_pattern': 'services/*', 'case_sensitive': True, 'limit': 10})
        assert {item['file_path'] for item in result.locations} == {'services/image.py', 'services/payment.ts'}
        assert result.search_scope['impact_coverage'] == 'unknown'
        assert result.search_scope['commit_sha'] == incident.commit_sha
        assert result.search_scope['navigation_only'] is True
        assert saved == [] and executor.added_evidence == [] and not session._regions
        read = executor.execute('read_repository_file', {'path': 'services/payment.ts', 'start_line': 1, 'end_line': 2})
        assert read.evidence[0]['evidence_id'] == saved[0].id
        assert saved[0].content == 'import {recover} from "../sdk/client";\nexport const pay = () => recover();'
        assert read.evidence[0]['material_role'] == 'static_candidate'
        assert saved[0].metadata['analysis'] == 'static_candidate'
        assert saved[0].commit_sha == incident.commit_sha


def test_find_catalog_read_ranges_and_symbol_lookup_share_one_snapshot(repository):
    incident, context, _ = repository
    with context.open_session(incident) as session:
        executor = executor_for(incident, session)
        files = executor.execute('find_repository_files', {'pattern': '.insight/*', 'limit': 10})
        assert files.locations == [{'file_path': '.insight/context.json'}]
        code = executor.execute('read_repository_file', {'path': 'sdk/client.py', 'start_line': 1, 'end_line': 2})
        related = executor.execute('get_related_code', {'symbol': 'recover', 'direction': 'both', 'limit': 6})
        assert code.status == related.status == 'ok'
        assert code.evidence[0]['evidence_id'] in related.reused_evidence_ids
        assert any('register' in e['content'] for e in related.evidence)
        assert len(session._regions['sdk/client.py']) == 2


@pytest.mark.parametrize('path', ['../outside.py', '/etc/passwd', 'C:/private.txt', 'sdk\\client.py', 'missing.py'])
def test_read_rejects_non_snapshot_paths(repository, path):
    incident, context, _ = repository
    with context.open_session(incident) as session:
        result = executor_for(incident, session).execute('read_repository_file', {'path': path, 'start_line': 1, 'end_line': 2})
        assert result.error.code == 'INVALID_PATH'
        assert not result.evidence


def test_read_rejects_resolved_escape_even_when_in_index(repository, tmp_path):
    incident, context, root = repository
    outside = tmp_path.parent / (tmp_path.name + '-outside.py')
    outside.write_text('private = 42', encoding='utf-8')
    try:
        link = root / 'link.py'
        link.symlink_to(outside)
        with context.open_session(incident) as session:
            result = executor_for(incident, session).execute('read_repository_file', {'path': 'link.py', 'start_line': 1, 'end_line': 1})
            assert result.error.code == 'INVALID_PATH'
    finally:
        outside.unlink()


def test_literal_search_bounds_and_empty_do_not_establish_impact_absence(repository, monkeypatch):
    incident, context, _ = repository
    with context.open_session(incident) as session:
        executor = executor_for(incident, session)
        literal = executor.execute('search_repository_code', {'query': 'foo.*', 'file_pattern': '**/*.md', 'case_sensitive': True, 'limit': 2})
        assert literal.locations[0]['line'] == 2
        empty = executor.execute('search_repository_code', {'query': 'not-in-snapshot', 'file_pattern': '*', 'case_sensitive': False, 'limit': 10})
        assert empty.status == 'empty' and empty.search_scope['scan_completed'] is True
        assert empty.search_scope['impact_coverage'] == 'unknown'
        monkeypatch.setattr(SnapshotSearch, 'MAX_SCAN_BYTES', 5)
        bounded = session.search_code('recover', '*', True, 10, time.monotonic() + 3)
        assert bounded['truncated'] is True and not bounded['search_scope']['scan_completed']
        timed = session.search_code('recover', '*', True, 10, time.monotonic() - 1)
        assert timed['truncated'] is True and timed['search_scope']['scanned_files'] == 0


def test_navigation_budget_cache_and_invalid_parameters(repository):
    incident, context, _ = repository
    with context.open_session(incident) as session:
        executor = executor_for(incident, session, output_budget_chars=65)
        args = {'pattern': '*', 'limit': 40}
        found = executor.execute('find_repository_files', args)
        assert found.truncated and executor.used_chars <= 65
        used = executor.used_chars
        cached = executor.execute('find_repository_files', args)
        assert cached.cache_hit and not cached.locations and executor.used_chars == used
        for tool, args in [('find_repository_files', {'pattern': '*', 'limit': True}),
                           ('search_repository_code', {'query': 'recover', 'file_pattern': '*', 'case_sensitive': True, 'limit': 3, 'repo_id': 'foreign'})]:
            assert executor.execute(tool, args).error.code == 'INVALID_ARGUMENTS'


def test_read_limits_and_failed_persistence(repository):
    incident, context, _ = repository
    with context.open_session(incident) as session:
        def fail(_items):
            raise OSError('storage failure')
        executor = executor_for(incident, session, persist_evidence=fail)
        for start, end in [(2, 1), (1, 121)]:
            assert executor.execute('read_repository_file', {'path': 'sdk/client.py', 'start_line': start, 'end_line': end}).error.code == 'INVALID_ARGUMENTS'
        with pytest.raises(ToolPersistenceError):
            executor.execute('read_repository_file', {'path': 'sdk/client.py', 'start_line': 1, 'end_line': 2})


def impact_fixture():
    from test_retrospective_pipeline import FakeCollaboration, FakeGit
    incident = IncidentRecord(title='upload')
    evidence = FakeGit().collect(incident) + FakeCollaboration().collect(incident)
    document, _ = FakeGenerator().generate(incident, evidence, [])
    static = Evidence(id='unreported-consumer', incident_id=incident.id,
        source_type=EvidenceSource.REPOSITORY_CODE, location='payment.py#L1', content='payment_client.recover()')
    document.impact.scope_basis = 'reported'
    document.impact.citations = [Citation(evidence_id=evidence[-1].id, quote='补充校验后恢复正常')]
    document.impact.coverage = 'partial'
    document.impact.potential_impacts = [PotentialImpact(content='若支付服务使用同一故障 SDK 版本，恢复路径可能同样受影响。',
        citations=[Citation(evidence_id=static.id, quote=static.content)],
        verification_needed='核对支付服务事故窗口的部署 SHA、SDK 版本及恢复日志/成功率。')]
    return incident, document, [*evidence, static]


def test_partial_chat_keeps_candidate_unverified_and_all_citations_checked():
    _, document, evidence = impact_fixture()
    report = QualityValidator().validate(document, evidence, [], evidence)
    assert report.hard_passed
    assert document.impact.potential_impacts[0].status == 'unverified'
    assert 'unreported-consumer' in {c.evidence_id for c in _document_citations(document)}
    document.impact.potential_impacts[0].citations[0].quote = 'fabricated quote'
    assert any('Quote not found' in e for e in QualityValidator().validate(document, evidence, [], evidence).hard_errors)


@pytest.mark.parametrize('basis', ['observed', 'reported'])
def test_static_candidate_cannot_be_relabelled_observed_or_reported(basis):
    _, document, evidence = impact_fixture()
    document.impact.scope_basis = basis
    document.impact.citations = document.impact.potential_impacts[0].citations
    assert not QualityValidator().validate(document, evidence, [], evidence).hard_passed


def test_legacy_documents_default_to_unknown_coverage_and_candidates_need_validation():
    _, document, _ = impact_fixture()
    legacy = document.impact.model_dump(exclude={'scope_basis', 'coverage', 'coverage_notes', 'potential_impacts'})
    migrated = ImpactSection.model_validate(legacy)
    assert migrated.coverage == migrated.scope_basis == 'unknown'
    assert migrated.potential_impacts == []
    with pytest.raises(ValidationError):
        PotentialImpact(content='payment is down', citations=document.impact.citations, status='confirmed', verification_needed='logs')
    with pytest.raises(ValidationError):
        ImpactSection.model_validate({**legacy, 'coverage': 'complete'})


def test_publish_preserves_candidates_without_putting_them_into_factual_history():
    _, document, _ = impact_fixture()
    markdown = _document_markdown(document)
    assert '待验证影响（不代表已发生）' in markdown and '部署 SHA' in markdown
    chunks = _knowledge_chunks(document)
    assert all('支付服务' not in text for text, _ in chunks)
    assert all('unreported-consumer' not in meta['source_evidence_ids'] for _, meta in chunks)
    assert any('覆盖说明' in text and 'partial' in text for text, _ in chunks)


def test_initial_and_writer_inputs_expose_roles_and_non_exhaustive_coverage():
    incident, document, evidence = impact_fixture()
    generator = OpenAIReviewGenerator(api_key='fixture-key', model='fixture-model')
    request = generator.build_request(incident, evidence, [])
    payload = json.loads(request['input'])
    assert 'potential_impacts' in request['instructions'] and '未提及' in request['instructions']
    assert payload['evidence'][-1]['material_role'] == 'static_candidate'
    assert payload['evidence'][-2]['material_role'] == 'participant_statement'
    initial = investigation_input(InvestigationContext(incident, evidence, evidence, [], None, generator, lambda *_: []))
    assert all(e['impact_coverage'] == 'unknown' for e in initial['evidence'])
