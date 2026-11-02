"""Offline review reminders, feedback-driven reinvestigation and approval gates."""

import importlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from repo_maintainer.investigation_runner import InvestigationContext, investigation_input
from repo_maintainer.retrospective_api import mount_retrospective_api
from repo_maintainer.retrospective_connectors import OpenAIReviewGenerator
from repo_maintainer.retrospective_code_context import RepositoryContextError
from repo_maintainer.retrospective_models import IncidentCreate, IncidentStatus, PublishResult
from repo_maintainer.retrospective_notifications import ReviewNotifier
from repo_maintainer.retrospective_pipeline import PipelineError
from repo_maintainer.retrospective_store import PostgresRetrospectiveStore
from test_retrospective_pipeline import FakeGenerator, make_pipeline


class ReviewLoopTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = make_pipeline()
        self.sender = Mock()
        self.retry = Mock()
        self.notifier = ReviewNotifier(self.pipeline.store, self.sender, "https://review.example.test", self.retry)
        self.pipeline.review_notifier = self.notifier
        self.incident = self.pipeline.create_incident(IncidentCreate(title="upload failure", feishu_chat_ids=["oc-review"]))

    def test_initial_and_revised_versions_each_notify_once_with_page_link(self):
        first = self.pipeline.run(self.incident.id)
        self.notifier.notify(self.incident.id, first.id)
        self.assertEqual(self.sender.send_text.call_count, 1)
        chat, text, first_uuid = self.sender.send_text.call_args.args
        self.assertEqual(chat, "oc-review")
        self.assertIn(f"https://review.example.test/retrospective?incident_id={self.incident.id}", text)
        self.assertIn("审核", text)
        self.pipeline.reject(self.incident.id, "alice", "引用需要核对")
        second = self.pipeline.regenerate(self.incident.id)
        self.assertEqual(self.sender.send_text.call_count, 2)
        self.assertNotEqual(self.sender.send_text.call_args.args[2], first_uuid)
        self.assertEqual(second.version, 2)
        self.assertFalse(self.pipeline.store._knowledge)

    def test_notification_failure_keeps_pending_draft_and_retry_reuses_uuid(self):
        self.sender.send_text.side_effect = [RuntimeError("private-provider-error"), None]
        version = self.pipeline.run(self.incident.id)
        current = self.pipeline.store.get_incident(self.incident.id)
        self.assertEqual(current.status, IncidentStatus.PENDING_REVIEW)
        self.assertNotIn("private-provider-error", current.metadata['review_notification_error'])
        self.retry.assert_called_once_with(self.incident.id, version.id)
        self.notifier.notify(self.incident.id, version.id)
        self.assertEqual(self.sender.send_text.call_args_list[0].args[2], self.sender.send_text.call_args_list[1].args[2])
        self.assertEqual(len(self.pipeline.store.list_versions(self.incident.id)), 1)
        self.assertEqual(self.pipeline.store.get_incident(self.incident.id).metadata['review_notification_error'], "")

    def test_partial_delivery_retries_only_unsent_groups(self):
        self.pipeline.store.update_incident(self.incident.id, feishu_chat_ids=["oc-a", "oc-b", "oc-a"])
        self.sender.send_text.side_effect = [None, RuntimeError("timeout"), None]
        version = self.pipeline.run(self.incident.id)
        self.notifier.notify(self.incident.id, version.id)
        self.assertEqual([c.args[0] for c in self.sender.send_text.call_args_list], ["oc-a", "oc-b", "oc-b"])

    def test_origin_chat_is_the_only_notification_target(self):
        self.pipeline.store.update_incident(self.incident.id, feishu_chat_ids=["oc-other"], metadata={"feishu_chat_id": "oc-origin"})
        self.pipeline.run(self.incident.id)
        self.assertEqual(self.sender.send_text.call_args.args[0], "oc-origin")

    def test_missing_public_url_records_actionable_error_without_sending_or_failing_draft(self):
        self.notifier.public_base_url = ""
        self.pipeline.run(self.incident.id)
        current = self.pipeline.store.get_incident(self.incident.id)
        self.assertEqual(current.status, IncidentStatus.PENDING_REVIEW)
        self.assertIn("REVIEW_PUBLIC_BASE_URL", current.metadata['review_notification_error'])
        self.sender.send_text.assert_not_called()

    def test_stale_notification_does_not_send_after_edit_or_approval(self):
        first = self.pipeline.run(self.incident.id)
        second = self.pipeline.edit(self.incident.id, first.document, "alice")
        self.assertEqual(self.notifier.notify(self.incident.id, first.id), {"status": "skipped"})
        self.pipeline.approve(self.incident.id, "alice")
        self.assertEqual(self.notifier.notify(self.incident.id, second.id), {"status": "skipped"})
        self.assertEqual(self.sender.send_text.call_count, 1)

    def test_feedback_and_old_draft_reach_writer_but_never_become_evidence(self):
        class RecordingGenerator(FakeGenerator):
            def generate(inner, incident, evidence, history):
                inner.incident = incident
                return super().generate(incident, evidence, history)
        generator = RecordingGenerator()
        self.pipeline.generator = generator
        first = self.pipeline.run(self.incident.id)
        self.pipeline.reject(self.incident.id, "alice", "请核对根因引用的版本")
        self.pipeline.regenerate(self.incident.id)
        feedback = generator.incident.metadata['review_feedback']
        self.assertEqual(feedback['actor'], "alice")
        self.assertEqual(feedback['reason'], "请核对根因引用的版本")
        self.assertEqual(feedback['version_id'], first.id)
        self.assertEqual(json.loads(feedback['previous_document_json']), first.document.model_dump())
        self.assertNotIn('review_feedback', self.pipeline.store.get_incident(self.incident.id).metadata)
        self.assertTrue(all("请核对根因引用的版本" not in e.content for e in self.pipeline.store.list_evidence(self.incident.id)))
        with patch('openai.OpenAI'):
            writer = OpenAIReviewGenerator(api_key='offline', model='offline')
        evidence = self.pipeline.store.list_evidence(self.incident.id)
        request = writer.build_request(generator.incident, evidence, [])
        self.assertEqual(json.loads(request['input'])['incident']['review_feedback'], feedback)
        request = writer.build_tool_request(generator.incident, evidence, [], [], [])
        self.assertEqual(json.loads(request['input'][0]['content'])['review_feedback'], feedback)
        context = InvestigationContext(generator.incident, evidence, evidence, [], self.pipeline.store, writer, Mock())
        self.assertEqual(investigation_input(context)['review_feedback'], feedback)

    def test_recollection_preserves_old_source_when_connector_reuses_changed_id(self):
        first = self.pipeline.run(self.incident.id)
        old = self.pipeline.store.list_evidence(self.incident.id)
        original = self.pipeline.git.collect
        def recollect(incident):
            evidence = original(incident)
            evidence[0].content += "\nFreshly checked source"
            return evidence
        self.pipeline.git.collect = Mock(side_effect=recollect)
        self.pipeline.reject(self.incident.id, 'alice', '重新取证')
        second = self.pipeline.regenerate(self.incident.id)
        evidence = {e.id: e for e in self.pipeline.store.list_evidence(self.incident.id)}
        self.assertEqual(evidence[old[0].id].content, old[0].content)
        self.assertIn(old[0].id, first.evidence_ids)
        self.assertNotIn(old[0].id, second.evidence_ids)
        self.assertTrue(any('Freshly checked source' in evidence[eid].content for eid in second.evidence_ids))

    def test_unavailable_repository_does_not_persist_feedback_as_incident_material(self):
        self.pipeline.run(self.incident.id)
        self.pipeline.reject(self.incident.id, 'alice', '引用需重新核对')
        self.pipeline.store.update_incident(self.incident.id, repo_name='team/service', commit_sha='b' * 40)
        self.pipeline.code_context = SimpleNamespace(collect=Mock(side_effect=RepositoryContextError('snapshot unavailable')))
        self.pipeline.regenerate(self.incident.id)
        record = self.pipeline.store.get_incident(self.incident.id)
        self.assertNotIn('review_feedback', record.metadata)
        self.assertEqual(record.metadata['code_context_warning'], 'snapshot unavailable')

    def test_unapproved_version_cannot_publish_or_index_even_with_saved_publication(self):
        version = self.pipeline.run(self.incident.id)
        publication = PublishResult(remote_id='orphan', url='https://offline.test/wiki')
        self.pipeline.store.save_publication(version.id, publication)
        with self.assertRaises(PipelineError):
            self.pipeline.publish(self.incident.id, 'alice')
        with self.assertRaises(PipelineError):
            self.pipeline._index_published_version(self.incident, version, publication)
        self.assertFalse(self.pipeline.store._knowledge)
        self.pipeline.reject(self.incident.id, 'alice', '修订后再审核')
        revised = self.pipeline.regenerate(self.incident.id)
        with self.assertRaises(PipelineError):
            self.pipeline.publish(self.incident.id, 'alice')
        self.assertFalse(self.pipeline.store._knowledge)
        self.pipeline.approve(self.incident.id, 'alice')
        result = self.pipeline.publish(self.incident.id, 'alice')
        self.assertEqual(result['knowledge_id'], f'knowledge_{revised.id}')


class RevisionApiTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = make_pipeline()
        self.incident = self.pipeline.create_incident(IncidentCreate(title='upload'))
        self.version = self.pipeline.run(self.incident.id)
        self.queue = SimpleNamespace(enqueue_revision=Mock(return_value='revision-job'))
        app = FastAPI()
        app.state.retrospective_pipeline = self.pipeline
        app.state.retrospective_queue = self.queue
        mount_retrospective_api(app)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.base = f'/api/retrospective/incidents/{self.incident.id}'
        self.payload = {'actor': 'alice', 'reason': '重新检查根因引用', 'version_id': self.version.id}

    def test_revision_is_queued_with_audited_feedback_and_no_knowledge(self):
        response = self.client.post(self.base + '/revise', json=self.payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.queue.enqueue_revision.assert_called_once_with(self.incident.id, self.version.id)
        record = self.pipeline.store.get_incident(self.incident.id)
        self.assertEqual(record.status, IncidentStatus.REJECTED)
        self.assertEqual(record.metadata['revision_job_status'], 'queued')
        self.assertEqual(self.pipeline.store.list_review_actions(self.incident.id)[-1].reason, self.payload['reason'])
        self.assertFalse(self.pipeline.store._knowledge)
        self.assertEqual(self.client.post(self.base + '/revise', json=self.payload).status_code, 409)

    def test_stale_page_cannot_approve_edit_or_request_agent_revision(self):
        self.pipeline.edit(self.incident.id, self.version.document, 'bob')
        for endpoint in ('approve', 'reject', 'publish', 'revise'):
            response = self.client.post(self.base + '/' + endpoint, json=self.payload)
            self.assertEqual(response.status_code, 409, response.text)
        response = self.client.patch(self.base + '/document', json={**self.payload, 'document': self.version.document.model_dump()})
        self.assertEqual(response.status_code, 409)
        self.queue.enqueue_revision.assert_not_called()

    def test_blank_feedback_and_shared_lock_block_revision(self):
        self.assertEqual(self.client.post(self.base + '/revise', json={**self.payload, 'reason': '  '}).status_code, 409)
        with self.pipeline.store.execution_lock(self.incident.id):
            self.assertEqual(self.client.post(self.base + '/revise', json=self.payload).status_code, 409)
        self.queue.enqueue_revision.assert_not_called()

    def test_enqueue_failure_retains_draft_and_allows_resubmission(self):
        self.queue.enqueue_revision.side_effect = ConnectionError('offline broker')
        self.assertEqual(self.client.post(self.base + '/revise', json=self.payload).status_code, 502)
        self.queue.enqueue_revision.side_effect = None
        self.assertEqual(self.client.post(self.base + '/revise', json=self.payload).status_code, 202)
        self.assertEqual(len(self.pipeline.store.list_versions(self.incident.id)), 1)


class RevisionTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'REDIS_URL': 'memory://'}):
            cls.tasks = importlib.import_module('repo_maintainer.retrospective_tasks')
        cls.app = cls.tasks.celery_app
        settings = {'broker_url': 'memory://', 'result_backend': 'cache+memory://',
                    'task_always_eager': True, 'task_eager_propagates': False, 'task_ignore_result': True}
        cls.old = {key: cls.app.conf[key] for key in settings}
        cls.app.conf.update(settings)

    @classmethod
    def tearDownClass(cls):
        cls.app.conf.update(cls.old)

    def setUp(self):
        self.pipeline = make_pipeline()
        self.incident = self.pipeline.create_incident(IncidentCreate(title='upload'))
        self.version = self.pipeline.run(self.incident.id)
        self.pipeline.reject(self.incident.id, 'alice', '重新查证')
        factory = patch.object(self.tasks, 'create_pipeline_from_env', return_value=self.pipeline)
        factory.start()
        self.addCleanup(factory.stop)

    def test_worker_revises_once_and_redelivery_skips_completed_version(self):
        task = self.tasks.revise_retrospective
        result = task.apply(args=[self.incident.id, self.version.id], task_id='job-revision', throw=True)
        self.assertTrue(result.successful())
        self.assertEqual(self.pipeline.store.get_incident(self.incident.id).status, IncidentStatus.PENDING_REVIEW)
        self.assertEqual(self.pipeline.store.get_incident(self.incident.id).metadata['revision_job_status'], 'completed')
        result = task.apply(args=[self.incident.id, self.version.id], task_id='job-duplicate', throw=True)
        self.assertEqual(result.result['status'], 'skipped')
        self.assertEqual(len(self.pipeline.store.list_versions(self.incident.id)), 2)
        self.assertFalse(self.pipeline.store._knowledge)

    def test_worker_failure_preserves_reviewable_version_and_feedback_for_retry(self):
        self.pipeline.generator.generate = Mock(side_effect=RuntimeError('offline model'))
        result = self.tasks.revise_retrospective.apply(args=[self.incident.id, self.version.id], task_id='job-failed', throw=False)
        self.assertTrue(result.failed())
        record = self.pipeline.store.get_incident(self.incident.id)
        self.assertEqual((record.status, record.metadata['revision_job_status']), (IncidentStatus.FAILED, 'failed'))
        self.assertEqual(self.pipeline._review_feedback(record)['reason'], '重新查证')
        self.assertEqual(len(self.pipeline.store.list_versions(self.incident.id)), 1)

    def test_notification_task_retry_does_not_regenerate(self):
        self.pipeline.store.update_incident(self.incident.id, status=IncidentStatus.PENDING_REVIEW, feishu_chat_ids=['oc-review'])
        sender = Mock()
        sender.send_text.side_effect = [RuntimeError('offline'), None]
        self.pipeline.review_notifier = ReviewNotifier(self.pipeline.store, sender, 'https://review.example.test')
        result = self.tasks.notify_retrospective_review.apply(args=[self.incident.id, self.version.id], throw=False)
        self.assertTrue(result.successful(), str(result.result))
        self.assertEqual(sender.send_text.call_count, 2)
        self.assertEqual(len(self.pipeline.store.list_versions(self.incident.id)), 1)


@unittest.skipUnless(os.getenv('TEST_CONTEXT_DATABASE_URL'), 'isolated PostgreSQL not configured')
class PostgresReviewLoopTests(unittest.TestCase):
    def test_review_feedback_notifications_and_approval_gate_survive_store_reload(self):
        url = os.environ['TEST_CONTEXT_DATABASE_URL']
        store = PostgresRetrospectiveStore(url)
        store.initialize()
        pipeline = make_pipeline(store=store)
        sender = Mock()
        pipeline.review_notifier = ReviewNotifier(store, sender, 'https://review.example.test')
        repo_id = 'review-loop-' + uuid4().hex
        incident = pipeline.create_incident(IncidentCreate(title='upload', repo_id=repo_id, feishu_chat_ids=['oc-review']))
        first = pipeline.run(incident.id)
        reloaded = PostgresRetrospectiveStore(url)
        self.assertTrue(reloaded.get_incident(incident.id).metadata['review_notifications'])
        self.assertEqual(reloaded.search_knowledge('upload', pipeline.embedder.embed_texts(['upload'])[0], repo_id=repo_id), [])
        pipeline.reject(incident.id, 'alice', '请重新核对引用')
        second = pipeline.regenerate(incident.id)
        self.assertEqual(len(reloaded.list_versions(incident.id)), 2)
        snapshots = reloaded.list_generation_contexts(incident.id)
        self.assertTrue(any(c.payload.get('request', {}).get('incident', {}).get('metadata', {}).get('review_feedback', {}).get('reason') == '请重新核对引用' for c in snapshots))
        with self.assertRaises(PipelineError):
            pipeline.publish(incident.id, 'alice')
        pipeline.approve(incident.id, 'alice')
        pipeline.publish(incident.id, 'alice')
        hits = reloaded.search_knowledge('upload', pipeline.embedder.embed_texts(['upload'])[0], repo_id=repo_id)
        self.assertEqual(hits[0].knowledge_id, f'knowledge_{second.id}')
        self.assertNotEqual(first.id, second.id)
