import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from repo_maintainer.config import GraphConfig
from repo_maintainer.repo_indexer import RepositoryIndexer
from repo_maintainer.retrospective_code_context import RepositoryCodeContext, RepositoryContextError
from repo_maintainer.retrospective_discovery import PythonSymbolIndex
from repo_maintainer.retrospective_models import Evidence, EvidenceSource, IncidentRecord


class RepositoryToolSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incident = IncidentRecord(title='registration recovery', repo_name='team/service',
                                       repo_id='team/service', commit_sha='a' * 40,
                                       metadata={'head_sha': 'b' * 40})
        self.opened = self.closed = 0

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def collector(self, **kwargs):
        test = self
        class Source:
            @contextmanager
            def open(self, incident):
                test.opened += 1
                try:
                    yield test.root
                finally:
                    test.closed += 1
        return RepositoryCodeContext(
            Source(), indexer=RepositoryIndexer(GraphConfig(enable_treesitter=False)), **kwargs)

    def diff(self, path='watcher.py', line=4):
        return Evidence(incident_id=self.incident.id, source_type=EvidenceSource.GITHUB_DIFF,
                        location=path, content=f'@@ -{line},1 +{line},1 @@\n+    return register()')

    def seed_chain(self):
        self.write('registration.py', 'def register():\n    return "registered"\n')
        self.write('watcher.py', 'from registration import register\n\n'
                   'def watcher():\n    return register()\n')
        self.write('entry.py', 'from watcher import watcher\n\n'
                   'def start():\n    return watcher()\n')

    def test_archive_and_indexes_are_reused_for_collection_and_tools(self):
        self.seed_chain()
        collector = self.collector()
        with patch.object(collector.indexer, 'index', wraps=collector.indexer.index) as repository_index, \
             patch('repo_maintainer.retrospective_code_context.PythonSymbolIndex', wraps=PythonSymbolIndex) as python_index:
            with collector.open_session(self.incident) as session:
                original = session.collect([self.diff()], allow_planner=False)
                callers = session.get_related_code('watcher', 'callers', 6)
                callees = session.get_related_code('watcher', 'callees', 6)
                self.assertTrue(original)
                self.assertTrue(any('def start' in item.content for item in callers.evidence))
                self.assertTrue(any('def register' in item.content for item in callees.evidence))
                self.assertEqual(self.opened, 1)
                self.assertEqual(self.closed, 0)
            self.assertEqual(repository_index.call_count, 1)
            self.assertEqual(python_index.call_count, 1)
        self.assertEqual(self.closed, 1)

    def test_code_results_use_fixed_head_and_reuse_existing_evidence_ids(self):
        self.seed_chain()
        with self.collector().open_session(self.incident) as session:
            first = session.get_related_code('watcher.py:watcher', 'both')
            self.incident.metadata['head_sha'] = 'c' * 40
            again = session.get_related_code('watcher', 'both')
            self.assertEqual({item.id for item in first.evidence}, {item.id for item in again.evidence})
            self.assertTrue(all(item.commit_sha == 'b' * 40 for item in again.evidence))
            self.assertTrue(all('b' * 40 in item.source_url for item in again.evidence))
            first.evidence[0].content = 'mutated by caller'
            self.assertNotEqual(session.get_related_code('watcher').evidence[0].content, 'mutated by caller')

    def test_ambiguity_unknown_and_invalid_requests_do_not_read_arbitrary_paths(self):
        self.write('one.py', 'class One:\n    def recover(self):\n        return 1\n')
        self.write('two.py', 'class Two:\n    def recover(self):\n        return 2\n')
        with self.collector().open_session(self.incident) as session:
            ambiguous = session.get_related_code('recover')
            self.assertEqual(ambiguous.status, 'ambiguous')
            self.assertEqual(ambiguous.evidence, [])
            self.assertEqual(ambiguous.candidates, ['one.py:One.recover', 'two.py:Two.recover'])
            self.assertEqual(session.get_related_code('One.recover').status, 'ok')
            self.assertEqual(session.get_related_code('../secret').status, 'empty')
            self.assertEqual(session.get_related_code('recover', 'unbounded').status, 'error')
            self.assertEqual(session.get_related_code('recover', limit=7).status, 'error')

    def test_file_region_and_result_limits_are_cumulative(self):
        self.write('one.py', 'def first():\n    return 1\n\ndef second():\n    return 2\n')
        self.write('two.py', 'def third():\n    return 3\n')
        collector = self.collector(max_files=1, max_expanded_files=1, max_regions_per_file=1)
        with collector.open_session(self.incident) as session:
            self.assertEqual(session.get_related_code('first').status, 'ok')
            self.assertEqual(session.get_related_code('second').status, 'budget_exhausted')
            self.assertEqual(session.get_related_code('third').status, 'budget_exhausted')
            self.assertEqual(session.get_related_code('first').status, 'ok')
        self.seed_chain()
        with self.collector().open_session(self.incident) as session:
            limited = session.get_related_code('watcher', 'both', 1)
            self.assertEqual(len(limited.evidence), 1)
            self.assertTrue(limited.truncated)

    def test_static_collection_also_consumes_session_region_quota(self):
        self.write('watcher.py', 'def changed():\n    return 1\n\ndef untouched():\n    return 2\n')
        with self.collector(max_regions_per_file=1).open_session(self.incident) as session:
            original = session.collect([self.diff(line=2)])
            self.assertTrue(original)
            reused = session.get_related_code('changed')
            self.assertEqual(reused.evidence[0].id, original[0].id)
            self.assertEqual(session.get_related_code('untouched').status, 'budget_exhausted')

    def test_tool_mode_can_disable_legacy_planner_without_mutating_owner(self):
        self.write('watcher.py', 'class Client:\n    def watcher(self):\n        return self.channel.reset()\n')
        planner = Mock(side_effect=AssertionError('legacy planner must not run'))
        collector = self.collector(planner=planner)
        with collector.open_session(self.incident) as session:
            session.collect([self.diff(line=3)], allow_planner=False)
        planner.assert_not_called()
        self.assertIs(collector.planner, planner)

    def test_long_source_is_explicitly_truncated(self):
        self.write('watcher.py', 'def watcher():\n' + ''.join(f'    value_{i} = {i}\n' for i in range(100)))
        with self.collector(max_snippet_chars=300).open_session(self.incident) as session:
            result = session.get_related_code('watcher')
            self.assertEqual(result.status, 'ok')
            self.assertTrue(result.truncated)
            self.assertFalse(result.evidence[0].metadata['region_complete'])

    def test_failed_or_finished_sessions_close_the_source(self):
        self.seed_chain()
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            with self.collector().open_session(self.incident) as session:
                raise RuntimeError('stop')
        self.assertEqual(self.closed, 1)
        with self.assertRaises(RepositoryContextError):
            session.get_related_code('watcher')
        with self.assertRaises(RepositoryContextError):
            session.collect([])
