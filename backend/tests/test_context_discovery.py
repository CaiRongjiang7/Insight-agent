import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repo_maintainer.retrospective_code_context import RepositoryCodeContext
from repo_maintainer.retrospective_discovery import CodeDiscoveryPlan, PythonSymbolIndex, service_map_context
from repo_maintainer.retrospective_models import Evidence, EvidenceSource, IncidentRecord
from repo_maintainer.retrospective_store import PostgresRetrospectiveStore, record_generation_context
from test_retrospective_pipeline import make_pipeline, FakeGenerator


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incident = IncidentRecord(title='stream watcher recovery', repo_name='team/sdk',
                                       repo_id='team/sdk', commit_sha='a' * 40)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def collector(self, **kwargs):
        root = self.root
        class Source:
            @contextmanager
            def open(self, incident):
                yield root
        return RepositoryCodeContext(Source(), **kwargs)

    def diff(self, path, line):
        return Evidence(incident_id=self.incident.id, source_type=EvidenceSource.GITHUB_DIFF,
                        location=path, content=f'@@ -{line},1 +{line},1 @@\n+    await self.reset()')

    def test_same_file_methods_and_task_creator_are_retrieved(self):
        self.write('client.py', 'class Client:\n'
                   '    async def watcher(self):\n        await self.reset()\n'
                   '    async def reset(self):\n        await self.register()\n'
                   '    async def register(self):\n        return True\n'
                   '    def start(self):\n        asyncio.create_task(self.watcher())\n')
        code = self.collector(max_regions_per_file=4).collect(self.incident, [self.diff('client.py', 3)])
        text = '\n'.join(e.content for e in code)
        for name in ('watcher', 'reset', 'register', 'start'):
            self.assertIn(f'def {name}(', text)
        self.assertTrue(all(e.metadata['region_complete'] for e in code))
        self.assertTrue(all(e.commit_sha == 'a' * 40 for e in code))

    def test_import_alias_inheritance_and_ambiguous_names(self):
        self.write('rpc.py', 'class Rpc:\n    def recover(self):\n        return 1\n')
        self.write('other.py', 'class Other:\n    def recover(self):\n        return 2\n')
        self.write('client.py', 'from rpc import Rpc as Base\nclass Client(Base):\n'
                   '    def watcher(self):\n        return self.recover()\n')
        index = PythonSymbolIndex(self.root, {'rpc.py', 'other.py', 'client.py'})
        watcher = index.lookup('Client.watcher')[0]
        self.assertEqual(index.resolve(watcher, 'self.recover').path, 'rpc.py')
        self.assertIsNone(index.resolve(watcher, 'unknown.recover'))
        self.assertIsNone(index.resolve(watcher, 'self.channel.recover'))
        self.assertEqual(index.lookup('recover'), [])

    def test_no_pr_stack_can_seed_code_discovery(self):
        self.write('client.py', 'class Client:\n    def watcher(self):\n        return self.reset()\n'
                   '    def reset(self):\n        return 1\n')
        self.incident.logs = 'File "client.py", line 3\nRuntimeError: stream broken'
        code = self.collector().collect(self.incident, [])
        self.assertIn('def reset(', '\n'.join(e.content for e in code))

    def test_bounded_planner_fetches_only_unique_existing_symbols_once(self):
        self.write('client.py', 'class Client:\n    async def watcher(self):\n        await self.channel.reset()\n')
        self.write('recovery.py', 'def restore_registration():\n    return "restored"\n')
        planner = Mock(return_value=CodeDiscoveryPlan(symbols=['restore_registration', '../../secret'],
                                                      questions=['是否恢复了注册？']))
        code = self.collector(planner=planner).collect(self.incident, [self.diff('client.py', 3)])
        self.assertEqual(planner.call_count, 1)
        followup = [e for e in code if 'bounded_symbol_followup' in e.metadata['relation']]
        self.assertEqual(len(followup), 1)
        self.assertIn('def restore_registration', followup[0].content)
        self.assertLessEqual(len({e.location.split('#')[0] for e in code}), 10)

    def test_planner_failure_keeps_static_evidence(self):
        self.write('client.py', 'class Client:\n    async def watcher(self):\n        await self.channel.reset()\n')
        code = self.collector(planner=Mock(side_effect=TimeoutError())).collect(
            self.incident, [self.diff('client.py', 3)])
        self.assertTrue(code)
        self.assertEqual(code[0].metadata['context_discovery']['planner'], 'unavailable:TimeoutError')

    def test_service_map_reads_only_explicit_local_docs_and_does_not_claim_impact(self):
        self.write('demo/service.py', 'def run():\n    return 1\n')
        self.write('docs/sdk.md', '# SDK recovery\nDo not equate reconnection with registration.\n')
        self.write('.insight/context.json', json.dumps({'services': [
            {'id': 'inference', 'paths': ['demo'], 'dependencies': ['sdk']},
            {'id': 'sdk', 'paths': ['sdk'], 'docs': ['docs/sdk.md', '../secret']},
        ]}))
        files = {p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file()}
        docs, summary = service_map_context(self.root, files, {'demo/service.py'})
        self.assertIn('docs/sdk.md', docs)
        self.assertEqual(summary['impact'], 'unverified')
        self.assertEqual(summary['dependency_candidates'], ['sdk'])
        self.assertNotIn('../secret', docs)
        code = self.collector().collect(self.incident, [self.diff('demo/service.py', 2)])
        self.assertTrue(any(e.location.startswith('docs/sdk.md#') for e in code))

    def test_truncated_function_is_not_marked_complete(self):
        self.write('client.py', 'def watcher():\n' + ''.join(f'    value_{i} = {i}\n' for i in range(150)))
        code = self.collector(max_snippet_chars=300).collect(self.incident, [self.diff('client.py', 80)])
        self.assertTrue(code)
        self.assertFalse(code[0].metadata['region_complete'])

    def test_context_budget_clipping_clears_region_completeness_without_mutating_raw(self):
        from repo_maintainer.retrospective_pipeline import ContextSelector
        raw = Evidence(incident_id=self.incident.id, source_type=EvidenceSource.REPOSITORY_CODE,
                       location='client.py#L1-L300', content='value = 1\n' * 300,
                       metadata={'region_complete': True})
        selected = ContextSelector(budget_chars=2000, max_item_chars=1000).select(self.incident, [raw]).evidence
        self.assertFalse(selected[0].metadata['region_complete'])
        self.assertTrue(raw.metadata['region_complete'])


class ContextPersistenceTests(unittest.TestCase):
    def test_failed_generation_keeps_snapshot_before_call(self):
        generator = Mock(spec=FakeGenerator)
        generator.generate.side_effect = RuntimeError('provider unavailable')
        pipeline = make_pipeline(generator=generator)
        incident = pipeline.create_incident(IncidentRecord(title='upload'))
        with self.assertRaises(RuntimeError):
            pipeline.run(incident.id)
        snapshots = pipeline.store.list_generation_contexts(incident.id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].payload['scope'], 'generator_arguments')

    def test_exact_provider_request_snapshot_is_immutable_across_regeneration(self):
        base = FakeGenerator()
        class PreparedGenerator:
            def build_request(self, incident, evidence, history):
                self.args = incident, evidence, history
                return {'model': 'fake', 'input': 'selected input', 'store': False}
            def generate_request(self, request):
                self.request = request
                return base.generate(*self.args)
        generator = PreparedGenerator()
        pipeline = make_pipeline(generator=generator)
        incident = pipeline.create_incident(IncidentRecord(title='upload'))
        pipeline.run(incident.id)
        first = pipeline.store.list_generation_contexts(incident.id)[0]
        self.assertEqual(first.payload['request'], generator.request)
        original = first.model_dump_json()
        pipeline.reject(incident.id, 'tester', 'retry')
        pipeline.regenerate(incident.id)
        snapshots = pipeline.store.list_generation_contexts(incident.id)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(original, snapshots[0].model_dump_json())
        self.assertEqual(snapshots[1].payload['target_version'], 2)
        expected = hashlib.sha256(json.dumps(first.payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(first.content_hash, expected)

    @unittest.skipUnless(os.getenv('TEST_CONTEXT_DATABASE_URL'), 'isolated PostgreSQL not configured')
    def test_postgres_additive_schema_and_snapshot_roundtrip(self):
        store = PostgresRetrospectiveStore(os.environ['TEST_CONTEXT_DATABASE_URL'])
        store.initialize()
        store.initialize()
        incident = store.create_incident(IncidentRecord(title='isolated snapshot test'))
        record_generation_context(store, incident, 'retrospective', {'input': 'fixed original', 'store': False})
        snapshots = store.list_generation_contexts(incident.id)
        self.assertEqual(snapshots[0].payload['request']['input'], 'fixed original')
        self.assertTrue(snapshots[0].created_at)


class DiscoveryRuntimeTests(unittest.TestCase):
    def test_planning_adapter_has_small_budget_and_no_transport_retries(self):
        from repo_maintainer.retrospective_connectors import OpenAIReviewGenerator
        client = Mock()
        client.with_options.return_value = client
        client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(
            name='request_code_symbols', arguments='{"symbols":["Client.recover"],"questions":["谁恢复注册？"]}')])
        with patch('openai.OpenAI', return_value=client):
            generator = OpenAIReviewGenerator(api_key='test-only', model='test-only', enable_thinking=False)
        recorder = Mock()
        generator.discovery_recorder = recorder
        plan = generator.plan_code_context(IncidentRecord(title='failure'), [])
        self.assertEqual(plan.symbols, ['Client.recover'])
        client.with_options.assert_called_once_with(max_retries=0)
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request['max_output_tokens'], 700)
        self.assertEqual(request['timeout'], 15)
        self.assertFalse(request['store'])
        self.assertEqual(recorder.call_args.args[1], request)

    def test_model_planner_requires_explicit_enable(self):
        from repo_maintainer import retrospective_runtime as runtime
        required = ['DATABASE_URL', 'REDIS_URL', 'OPENAI_API_KEY', 'OPENAI_MODEL', 'EMBEDDING_MODEL',
                    'GITHUB_TOKEN', 'GITHUB_WEBHOOK_SECRET', 'FEISHU_APP_ID', 'FEISHU_APP_SECRET',
                    'FEISHU_WIKI_SPACE_ID', 'FEISHU_WIKI_PARENT_NODE_TOKEN']
        for setting in ('', '0', '1'):
            env = {k: 'test-only' for k in required}
            env['CODE_CONTEXT_PLANNER_ENABLED'] = setting
            with self.subTest(setting=setting), patch.dict(os.environ, env, clear=True), \
                 patch.object(runtime, 'PostgresRetrospectiveStore'), patch.object(runtime, 'RedisTokenCache'), \
                 patch.object(runtime, 'FeishuConnector'), patch.object(runtime, 'OpenAIEmbedder'), \
                 patch.object(runtime, 'OpenAIReviewGenerator'):
                pipeline = runtime.create_pipeline_from_env()
                self.assertEqual(pipeline.code_context.planner is not None, setting == '1')
