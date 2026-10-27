import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from repo_maintainer.retrospective_models import Evidence, EvidenceSource, IncidentRecord, KnowledgeHit
from repo_maintainer.retrospective_tools import ToolExecutor, ToolPersistenceError


class RetrospectiveToolTests(unittest.TestCase):
    def setUp(self):
        self.incident = IncidentRecord(id='incident-one', title='stream registration recovery',
                                       repo_id='team/service', commit_sha='a' * 40)
        self.code = Evidence(id='ev-code', incident_id=self.incident.id, source_type=EvidenceSource.REPOSITORY_CODE,
                             repo_id=self.incident.repo_id, commit_sha=self.incident.commit_sha,
                             location='client.py#L1-L5', content='async def recover():\n    await register()\n',
                             metadata={'region_complete': True})
        self.chat = Evidence(id='ev-chat', incident_id=self.incident.id, source_type=EvidenceSource.FEISHU_MESSAGE,
                             location='chat/message-1', content='图像服务掉线，捕获 RST_STREAM 后需要恢复注册。')
        self.hit = KnowledgeHit(knowledge_id='knowledge-one', incident_id='old-incident', title='Earlier stream fault',
                                content='Earlier stream recovered by reconnecting.', score=0.8,
                                metadata={'matched_section': 'fix_solution'})

    def session(self, **changes):
        lookup = SimpleNamespace(status='ok', evidence=[self.code], candidates=[], truncated=False, error='')
        for key, value in changes.items():
            setattr(lookup, key, value)
        return SimpleNamespace(get_related_code=Mock(return_value=lookup))

    def code_args(self):
        return {'symbol': 'Client.recover', 'direction': 'both', 'limit': 3}

    def test_definitions_have_strict_schema_and_validation_rejects_scope_injection(self):
        session = self.session()
        executor = ToolExecutor(self.incident, [], code_session=session)
        for definition in executor.definitions():
            self.assertTrue(definition['strict'])
            schema = definition['parameters']
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['required']), set(schema['properties']))
        result = executor.execute('get_related_code', {**self.code_args(), 'repo_id': 'foreign/repo'})
        self.assertEqual(result.error.code, 'INVALID_ARGUMENTS')
        self.assertEqual(executor.calls_used, 0)
        session.get_related_code.assert_not_called()
        self.assertEqual(executor.execute('__import__', {}).error.code, 'UNKNOWN_TOOL')
        self.assertEqual(executor.execute('get_related_code', '{bad').error.code, 'INVALID_ARGUMENTS')
        self.assertEqual(executor.execute('get_related_code', {**self.code_args(), 'limit': True}).error.code, 'INVALID_ARGUMENTS')

    def test_cached_call_does_not_execute_or_repeat_body(self):
        session = self.session()
        recorder = Mock()
        executor = ToolExecutor(self.incident, [], code_session=session, recorder=recorder)
        first = executor.execute('get_related_code', json.dumps(self.code_args()))
        used = executor.used_chars
        second = executor.execute('get_related_code', self.code_args())
        self.assertEqual(first.status, 'ok')
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.evidence, [])
        self.assertEqual(second.reused_evidence_ids, ['ev-code'])
        self.assertEqual(executor.used_chars, used)
        self.assertEqual(executor.calls_used, 1)
        session.get_related_code.assert_called_once()
        self.assertEqual(recorder.call_count, 2)

    def test_new_originals_are_saved_before_clipped_copy_without_mutating_raw(self):
        events = []
        persist = lambda evidence: events.append(('persist', evidence[0].content))
        record = lambda event: events.append(('record', event['result']['evidence'][0]['content']))
        executor = ToolExecutor(self.incident, [], code_session=self.session(), persist_evidence=persist,
                                recorder=record, output_budget_chars=10)
        result = executor.execute('get_related_code', self.code_args())
        self.assertEqual(events[0], ('persist', self.code.content))
        self.assertEqual(events[1], ('record', self.code.content[:10]))
        self.assertTrue(result.truncated)
        self.assertFalse(result.evidence[0]['region_complete'])
        self.assertEqual(executor.added_evidence[0].content, self.code.content)
        self.assertTrue(self.code.metadata['region_complete'])

    def test_shared_body_budget_applies_across_current_and_historical_material(self):
        history = Mock(return_value=[self.hit])
        executor = ToolExecutor(self.incident, [self.chat], history_search=history,
                                output_budget_chars=len(self.chat.content) + 7)
        chat = executor.execute('search_feishu_context', {'query': 'RST_STREAM', 'limit': 3})
        historical = executor.execute('search_historical_cases', {'query': 'stream', 'limit': 3})
        self.assertEqual(chat.evidence[0]['content'], self.chat.content)
        self.assertEqual(len(historical.historical_cases[0]['content']), 7)
        self.assertTrue(historical.truncated)
        self.assertEqual(executor.used_chars, len(self.chat.content) + 7)
        self.assertEqual(executor.execute('search_historical_cases', {'query': 'retry', 'limit': 3}).status, 'budget_exhausted')
        self.assertEqual(history.call_count, 1)

    def test_feishu_search_is_local_to_current_incident_and_supports_chinese(self):
        foreign = self.chat.model_copy(update={'id': 'foreign', 'incident_id': 'other-incident'})
        raw_code = self.code.model_copy(update={'content': 'RST_STREAM'})
        executor = ToolExecutor(self.incident, [self.chat, foreign, raw_code])
        result = executor.execute('search_feishu_context', {'query': '图像服务掉线如何恢复注册', 'limit': 3})
        self.assertEqual([e['evidence_id'] for e in result.evidence], ['ev-chat'])
        self.assertEqual(result.evidence[0]['material_role'], 'participant_statement')
        self.assertEqual(executor.execute('search_feishu_context', {'query': 'unrelated-topic', 'limit': 3}).status, 'empty')

    def test_historical_hits_never_become_current_evidence_and_skip_self_case(self):
        own = self.hit.model_copy(update={'knowledge_id': 'own', 'incident_id': self.incident.id})
        history = Mock(return_value=[self.hit, own])
        executor = ToolExecutor(self.incident, [], history_search=history)
        result = executor.execute('search_historical_cases', {'query': 'stream', 'limit': 5})
        self.assertEqual(result.evidence, [])
        self.assertNotIn('evidence_id', result.historical_cases[0])
        self.assertEqual(result.historical_cases[0]['material_role'], 'historical_analogy')
        self.assertEqual([h.knowledge_id for h in executor.historical_hits], ['knowledge-one'])
        self.assertEqual(executor.added_evidence, [])

    def test_long_document_and_history_return_the_matching_window(self):
        body = 'background\n' * 400 + 'RST_STREAM requires registration recovery.'
        doc = self.chat.model_copy(update={'source_type': EvidenceSource.FEISHU_DOC, 'content': body})
        hit = self.hit.model_copy(update={'content': body})
        executor = ToolExecutor(self.incident, [doc], history_search=Mock(return_value=[hit]))
        result = executor.execute('search_feishu_context', {'query': 'RST_STREAM', 'limit': 1})
        self.assertIn('RST_STREAM', result.evidence[0]['content'])
        self.assertGreater(result.evidence[0]['excerpt_start'], 0)
        result = executor.execute('search_historical_cases', {'query': 'RST_STREAM', 'limit': 1})
        self.assertIn('RST_STREAM', result.historical_cases[0]['content'])
        self.assertEqual(executor.historical_hits[0].content, result.historical_cases[0]['content'])

    def test_call_count_and_deadline_return_structured_budget_result(self):
        history = Mock(return_value=[])
        executor = ToolExecutor(self.incident, [], history_search=history, max_calls=4)
        for index in range(4):
            self.assertEqual(executor.execute('search_historical_cases', {'query': f'query-{index}', 'limit': 1}).status, 'empty')
        excess = executor.execute('search_historical_cases', {'query': 'query-4', 'limit': 1})
        self.assertEqual(excess.status, 'budget_exhausted')
        self.assertEqual(history.call_count, 4)
        timed = ToolExecutor(self.incident, [], history_search=history, timeout_seconds=0)
        self.assertEqual(timed.execute('search_historical_cases', {'query': 'timeout', 'limit': 1}).status, 'budget_exhausted')

    def test_ambiguity_and_unavailable_code_do_not_emit_evidence(self):
        executor = ToolExecutor(self.incident, [], code_session=self.session(status='ambiguous', candidates=['a:recover', 'b:recover']))
        result = executor.execute('get_related_code', self.code_args())
        self.assertEqual(result.status, 'ambiguous')
        self.assertEqual(result.candidates, ['a:recover', 'b:recover'])
        self.assertFalse(result.evidence)
        unavailable = ToolExecutor(self.incident, [])
        self.assertEqual(unavailable.execute('get_related_code', self.code_args()).error.code, 'CODE_UNAVAILABLE')

    def test_wrong_sha_or_conflicting_original_is_rejected(self):
        bad = self.code.model_copy(update={'commit_sha': 'b' * 40})
        executor = ToolExecutor(self.incident, [], code_session=self.session(evidence=[bad]))
        self.assertEqual(executor.execute('get_related_code', self.code_args()).error.code, 'INVALID_RESULT_SCOPE')
        old = self.code.model_copy(update={'content': 'old body'})
        executor = ToolExecutor(self.incident, [old], code_session=self.session())
        self.assertEqual(executor.execute('get_related_code', self.code_args()).error.code, 'EVIDENCE_ID_CONFLICT')

    def test_upstream_error_is_sanitized_and_persistence_failure_is_not_relabelled(self):
        history = Mock(side_effect=RuntimeError('secret-token-in-provider-error'))
        executor = ToolExecutor(self.incident, [], history_search=history)
        result = executor.execute('search_historical_cases', {'query': 'stream', 'limit': 3})
        self.assertEqual(result.error.code, 'TOOL_FAILED')
        self.assertNotIn('secret-token', result.model_dump_json())
        persist = Mock(side_effect=RuntimeError('database unavailable'))
        executor = ToolExecutor(self.incident, [], code_session=self.session(), persist_evidence=persist)
        with self.assertRaises(ToolPersistenceError):
            executor.execute('get_related_code', self.code_args())
        self.assertFalse(executor.added_evidence)
        recorder = Mock(side_effect=RuntimeError('trace unavailable'))
        executor = ToolExecutor(self.incident, [], recorder=recorder)
        with self.assertRaisesRegex(RuntimeError, 'trace unavailable'):
            executor.execute('unknown', {})
        recorder.assert_called_once()
