import json
import os
import unittest
from unittest.mock import Mock, patch

from repo_maintainer.retrospective_connectors import OpenAIReviewGenerator
from repo_maintainer.retrospective_models import Evidence, EvidenceSource, IncidentRecord
from repo_maintainer.retrospective_pipeline import ContextSelector
from test_retrospective_tool_loop import (
    FakeCodeContext, FakeRepositorySession, ToolLoopGenerator, function_call,
)


class StandardToolAdapterTests(unittest.TestCase):
    def generator(self):
        client = Mock()
        client.with_options.return_value = client
        with patch('openai.OpenAI', return_value=client):
            generator = OpenAIReviewGenerator(api_key='test-only', model='test-model', enable_thinking=False)
        return generator, client

    def test_small_bootstrap_and_followup_preserve_protocol_items(self):
        generator, client = self.generator()
        incident = IncidentRecord(title='failure')
        evidence = [Evidence(incident_id=incident.id, source_type=EvidenceSource.CI_LOG,
                             location=str(i), content='x' * 9_000) for i in range(20)]
        tools = [{'type': 'function', 'name': 'example', 'parameters': {'type': 'object'}}]
        request = generator.build_tool_request(incident, evidence, [], [], tools)
        payload = json.loads(request['input'][0]['content'])
        self.assertLessEqual(sum(len(e['content']) for e in payload['evidence']), 8_000)
        self.assertTrue(all(e['truncated'] for e in payload['evidence']))
        self.assertEqual(request['tool_choice'], 'auto')
        self.assertEqual(request['max_output_tokens'], 700)
        self.assertEqual(request['timeout'], 15)
        self.assertFalse(request['store'])
        messages = [{'type': 'reasoning', 'id': 'r1', 'summary': [], 'encrypted_content': 'opaque'},
                    {'type': 'function_call', 'call_id': 'c1', 'name': 'example', 'arguments': '{}'},
                    {'type': 'function_call_output', 'call_id': 'c1', 'output': '{"status":"empty"}'}]
        next_request = generator.build_tool_request(incident, evidence, [], messages, tools)
        self.assertEqual(next_request['input'], messages)
        generator.request_tool_calls(next_request)
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual(client.responses.create.call_args.kwargs, next_request)

    def test_final_writer_only_submits_and_receives_coverage_summary(self):
        generator, _ = self.generator()
        incident = IncidentRecord(title='failure', metadata={'tool_exploration': {'stop_reason': 'round_limit'}})
        request = generator.build_request(incident, [], [])
        self.assertEqual([t['name'] for t in request['tools']], ['submit_retrospective'])
        self.assertEqual(json.loads(request['input'])['incident']['tool_exploration']['stop_reason'], 'round_limit')

    def test_runtime_tools_are_opt_in_and_disable_legacy_planner(self):
        from repo_maintainer import retrospective_runtime as runtime
        required = ['DATABASE_URL', 'REDIS_URL', 'OPENAI_API_KEY', 'OPENAI_MODEL', 'EMBEDDING_MODEL',
                    'GITHUB_TOKEN', 'GITHUB_WEBHOOK_SECRET', 'FEISHU_APP_ID', 'FEISHU_APP_SECRET',
                    'FEISHU_WIKI_SPACE_ID', 'FEISHU_WIKI_PARENT_NODE_TOKEN']
        for setting in ('', '0', '1'):
            env = {key: 'test-only' for key in required}
            env.update(REVIEW_TOOLS_ENABLED=setting, CODE_CONTEXT_PLANNER_ENABLED='1')
            with self.subTest(setting=setting), patch.dict(os.environ, env, clear=True), \
                 patch.object(runtime, 'PostgresRetrospectiveStore'), patch.object(runtime, 'RedisTokenCache'), \
                 patch.object(runtime, 'FeishuConnector'), patch.object(runtime, 'OpenAIEmbedder'), \
                 patch.object(runtime, 'OpenAIReviewGenerator'):
                pipeline = runtime.create_pipeline_from_env()
                self.assertEqual(pipeline.tools_enabled, setting == '1')
                self.assertEqual(pipeline.code_context.planner is None, setting == '1')


class ToolSelectionRegressionTests(unittest.TestCase):
    def setup_pipeline(self, generator, context=None):
        from test_retrospective_tool_loop import RetrospectiveToolLoopTests
        helper = RetrospectiveToolLoopTests()
        pipeline = helper.pipeline(generator, context or FakeCodeContext())
        return pipeline, helper.incident(pipeline)

    def test_sdk_reasoning_item_is_retained_in_next_request(self):
        from openai.types.responses import ResponseReasoningItem
        reasoning = ResponseReasoningItem(id='reasoning-1', summary=[], encrypted_content='opaque-state', type='reasoning')
        generator = ToolLoopGenerator([[reasoning, function_call('chat', 'search_feishu_context',
                                      {'query': '上传', 'limit': 1})], []])
        pipeline, incident = self.setup_pipeline(generator)
        pipeline.run(incident.id)
        item = next(i for i in generator.requests[1]['input'] if i.get('type') == 'reasoning')
        self.assertEqual(item['encrypted_content'], 'opaque-state')

    def test_initial_selection_does_not_mark_omitted_final_material_as_selected(self):
        generator = ToolLoopGenerator([[], []])
        pipeline, incident = self.setup_pipeline(generator)
        original = pipeline.collaboration.collect
        pipeline.collaboration.collect = lambda inc: original(inc) + [Evidence(
            id='drop-after-tools', incident_id=inc.id, source_type=EvidenceSource.FEISHU_DOC,
            location='configured document', content='initial context only')]
        class Reselector:
            calls = 0
            def select(self, incident, evidence):
                self.calls += 1
                if self.calls > 1:
                    evidence = [e for e in evidence if e.id != 'drop-after-tools']
                return ContextSelector(budget_chars=6000, max_item_chars=2000).select(incident, evidence)
        pipeline.selector = Reselector()
        version = pipeline.run(incident.id)
        omitted = next(e for e in pipeline.store.list_evidence(incident.id) if e.id == 'drop-after-tools')
        self.assertFalse(omitted.selected)
        self.assertNotIn(omitted.id, version.evidence_ids)

    def test_tool_budget_clipping_remains_incomplete_in_final_input(self):
        generator = ToolLoopGenerator([
            [function_call('chat', 'search_feishu_context', {'query': 'recovery', 'limit': 3})],
            [function_call('code', 'get_related_code', {'symbol': 'Client.watcher', 'direction': 'both', 'limit': 1})],
        ], cite_code=True)
        class LargeSession(FakeRepositorySession):
            def get_related_code(self, symbol, direction='both', limit=6):
                result = super().get_related_code(symbol, direction, limit)
                result.evidence[0].content += '\n' + '# context\n' * 200
                return result
        context = FakeCodeContext()
        from contextlib import contextmanager
        @contextmanager
        def open_session(inc):
            yield LargeSession(context, inc)
        context.open_session = open_session
        pipeline, incident = self.setup_pipeline(generator, context)
        original = pipeline.collaboration.collect
        pipeline.collaboration.collect = lambda inc: original(inc) + [Evidence(
            id=f'long-chat-{i}', incident_id=inc.id, source_type=EvidenceSource.FEISHU_MESSAGE,
            location=f'authorized chat {i}', content='recovery 上传参数校验缺失 ' + 'x' * 5_000)
            for i in range(3)]
        pipeline.run(incident.id)
        selected = next(e for e in generator.final_inputs[0][1] if e.id.startswith('ev_tool_restore_'))
        raw = next(e for e in pipeline.store.list_evidence(incident.id) if e.id == selected.id)
        self.assertLess(len(selected.content), len(raw.content))
        self.assertFalse(selected.metadata['region_complete'])
        self.assertTrue(raw.metadata['region_complete'])

    @unittest.skipUnless(os.getenv('TEST_CONTEXT_DATABASE_URL'), 'isolated PostgreSQL not configured')
    def test_tool_loop_persists_requests_results_and_new_code_in_postgres(self):
        from repo_maintainer.retrospective_store import PostgresRetrospectiveStore
        from test_retrospective_tool_loop import RetrospectiveToolLoopTests
        store = PostgresRetrospectiveStore(os.environ['TEST_CONTEXT_DATABASE_URL'])
        store.initialize()
        generator = ToolLoopGenerator([
            [function_call('code', 'get_related_code', {'symbol': 'Client.watcher', 'direction': 'both', 'limit': 2})],
            [],
        ], cite_code=True)
        helper = RetrospectiveToolLoopTests()
        context = FakeCodeContext()
        pipeline = helper.pipeline(generator, context, store)
        incident = helper.incident(pipeline)
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(version.prompt_version, 'v3-standard-tools')
        snapshots = store.list_generation_contexts(incident.id)
        purposes = {s.payload['purpose'] for s in snapshots}
        self.assertTrue({'tool_exploration', 'tool_execution', 'tool_context_selection', 'retrospective'} <= purposes)
        self.assertTrue(all(s.content_hash for s in snapshots))
        stored = next(e for e in store.list_evidence(incident.id) if e.id.startswith('ev_tool_restore_'))
        self.assertTrue(stored.selected)
        self.assertIn('client.register_instance()', stored.content)
        self.assertEqual((context.opens, context.closes), (1, 1))
