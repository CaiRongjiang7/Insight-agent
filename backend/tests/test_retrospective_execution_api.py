"""Offline API regressions for per-incident execution and review exclusion."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from repo_maintainer.retrospective_api import mount_retrospective_api
from repo_maintainer.retrospective_models import (
    Citation,
    EvidenceBackedText,
    ImpactSection,
    IncidentRecord,
    IncidentStatus,
    PublishResult,
    QualityReport,
    RetrospectiveDocument,
    RetrospectiveVersion,
)
from repo_maintainer.retrospective_pipeline import PipelineError
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore


class RetrospectiveExecutionApiTests(unittest.TestCase):
    def setUp(self):
        # Leave local socket-pair setup used by Windows asyncio/TestClient intact.
        blocker = patch("repo_maintainer.retrospective_connectors.urlopen",
                        side_effect=AssertionError("Network disabled in API test"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.store = InMemoryRetrospectiveStore()
        self.incident = self.store.create_incident(IncidentRecord(id="inc-lock-test", title="配置回滚"))
        citation = Citation(evidence_id="ev-chat", quote="配置回滚后恢复")
        self.document = RetrospectiveDocument(
            title="配置回滚", summary="参与者报告配置回滚后恢复。",
            impact=ImpactSection(scope="图像推理", citations=[citation]),
            root_cause=EvidenceBackedText(content="配置异常待核对", citations=[citation]),
            fix_solution=EvidenceBackedText(content="参与者报告回滚", citations=[citation]),
            prevention=["增加配置审核"],
        )
        self.version = RetrospectiveVersion(incident_id=self.incident.id, version=1,
                                             document=self.document, quality=QualityReport())
        self.publication = PublishResult(remote_id="wiki-test", url="https://offline.invalid/wiki/test")
        self.pipeline = SimpleNamespace(
            store=self.store,
            retry=Mock(return_value=self.version), regenerate=Mock(return_value=self.version),
            edit=Mock(return_value=self.version), approve=Mock(return_value=self.incident),
            reject=Mock(return_value=self.incident),
            publish=Mock(return_value={"publication": self.publication, "knowledge_id": "knowledge-test"}),
        )
        self.queue = SimpleNamespace(enqueue=Mock(return_value="job-test"))
        # Only mount this API; avoid production application startup and external clients.
        self.app = FastAPI()
        self.app.state.retrospective_pipeline = self.pipeline
        self.app.state.retrospective_queue = self.queue
        mount_retrospective_api(self.app)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.base = f"/api/retrospective/incidents/{self.incident.id}"

    def assert_lock_available(self):
        with self.store.execution_lock(self.incident.id) as acquired:
            self.assertTrue(acquired, "API leaked the incident execution lock")

    def operation_holding_lock(self, result):
        def perform(*args, **kwargs):
            self.assertEqual(args[0], self.incident.id)
            with self.store.execution_lock(self.incident.id) as acquired:
                self.assertFalse(acquired, "Mutation ran outside the incident execution lock")
            return result
        return perform

    def review_request(self, action):
        if action == "edit":
            return self.client.patch(self.base + "/document", json={
                "actor": "reviewer", "document": self.document.model_dump(mode="json"),
            })
        return self.client.post(self.base + "/" + action, json={"actor": "reviewer", "reason": "verified"})

    def test_run_retry_and_regenerate_return_conflict_while_same_incident_is_locked(self):
        for action, state in (("run", IncidentStatus.RECEIVED), ("retry", IncidentStatus.FAILED),
                              ("regenerate", IncidentStatus.REJECTED)):
            with self.subTest(action=action):
                self.store.update_incident(self.incident.id, status=state)
                with self.store.execution_lock(self.incident.id) as acquired:
                    self.assertTrue(acquired)
                    response = self.client.post(self.base + "/" + action)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn("already being processed", response.json()["detail"])
                self.queue.enqueue.assert_not_called()
                self.pipeline.retry.assert_not_called()
                self.pipeline.regenerate.assert_not_called()
                self.assert_lock_available()

    def test_run_publishes_under_lock_then_releases_and_records_queued_job(self):
        self.queue.enqueue.side_effect = self.operation_holding_lock("job-test")
        response = self.client.post(self.base + "/run")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"incident_id": self.incident.id, "job_id": "job-test", "accepted": True})
        self.queue.enqueue.assert_called_once_with(self.incident.id)
        self.assertEqual(self.store._jobs["job-test"], {
            "incident_id": self.incident.id, "stage": "collecting", "status": "queued", "error": "",
        })
        self.assert_lock_available()

    def test_failed_incident_can_be_enqueued_again(self):
        self.store.update_incident(self.incident.id, status=IncidentStatus.FAILED)
        self.assertEqual(self.client.post(self.base + "/run").status_code, 202)
        self.queue.enqueue.assert_called_once_with(self.incident.id)
        self.assert_lock_available()

    def test_run_rejects_pending_review_or_other_nonstartable_states_without_enqueuing(self):
        for state in (IncidentStatus.PENDING_REVIEW, IncidentStatus.COLLECTING, IncidentStatus.GENERATING,
                      IncidentStatus.REJECTED, IncidentStatus.APPROVED, IncidentStatus.PUBLISHED):
            with self.subTest(state=state):
                self.store.update_incident(self.incident.id, status=state)
                response = self.client.post(self.base + "/run")
                self.assertEqual(response.status_code, 409, response.text)
                self.queue.enqueue.assert_not_called()
                self.assertEqual(self.store._jobs, {})
                self.assert_lock_available()

    def test_run_enqueue_failure_releases_lock_and_can_be_retried(self):
        self.queue.enqueue.side_effect = ConnectionError("offline broker")
        response = self.client.post(self.base + "/run")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.store._jobs, {})
        self.assert_lock_available()
        self.queue.enqueue.side_effect = self.operation_holding_lock("job-retry")
        response = self.client.post(self.base + "/run")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "job-retry")
        self.assert_lock_available()

    def test_lock_for_another_incident_does_not_block_current_incident(self):
        with self.store.execution_lock("another-incident") as acquired:
            self.assertTrue(acquired)
            response = self.client.post(self.base + "/run")
        self.assertEqual(response.status_code, 202, response.text)
        self.assert_lock_available()

    def test_missing_incident_is_not_enqueued_and_does_not_leak_lock(self):
        response = self.client.post("/api/retrospective/incidents/missing/run")
        self.assertEqual(response.status_code, 404, response.text)
        self.queue.enqueue.assert_not_called()
        with self.store.execution_lock("missing") as acquired:
            self.assertTrue(acquired)

    def test_missing_queue_returns_service_unavailable_without_mutating_incident(self):
        self.app.state.retrospective_queue = None
        response = self.client.post(self.base + "/run")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.store.get_incident(self.incident.id).status, IncidentStatus.RECEIVED)
        self.queue.enqueue.assert_not_called()
        self.assert_lock_available()

    def test_retry_and_regenerate_hold_lock_during_execution_and_release_after_success(self):
        for action, state in (("retry", IncidentStatus.FAILED), ("regenerate", IncidentStatus.REJECTED)):
            with self.subTest(action=action):
                self.store.update_incident(self.incident.id, status=state)
                method = getattr(self.pipeline, action)
                method.side_effect = self.operation_holding_lock(self.version)
                response = self.client.post(self.base + "/" + action)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["id"], self.version.id)
                method.assert_called_once_with(self.incident.id)
                self.assert_lock_available()

    def test_generation_endpoint_errors_release_lock(self):
        for action in ("retry", "regenerate"):
            for error, expected_status in ((PipelineError("invalid transition"), 409), (RuntimeError("offline model"), 502)):
                with self.subTest(action=action, error=type(error).__name__):
                    getattr(self.pipeline, action).side_effect = error
                    response = self.client.post(self.base + "/" + action)
                    self.assertEqual(response.status_code, expected_status, response.text)
                    self.assert_lock_available()

    def test_edit_approve_reject_publish_do_not_execute_while_incident_is_locked(self):
        for action in ("edit", "approve", "reject", "publish"):
            with self.subTest(action=action):
                with self.store.execution_lock(self.incident.id) as acquired:
                    self.assertTrue(acquired)
                    response = self.review_request(action)
                    self.assertEqual(response.status_code, 409, response.text)
                getattr(self.pipeline, action).assert_not_called()
                self.assert_lock_available()

    def test_review_mutations_hold_lock_and_release_after_success(self):
        results = {"edit": self.version, "approve": self.incident, "reject": self.incident,
                   "publish": {"publication": self.publication, "knowledge_id": "knowledge-test"}}
        for action, result in results.items():
            with self.subTest(action=action):
                getattr(self.pipeline, action).side_effect = self.operation_holding_lock(result)
                response = self.review_request(action)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_lock_available()
        self.assertEqual(response.json()["publication"]["remote_id"], "wiki-test")

    def test_review_mutation_failure_releases_lock(self):
        for action in ("edit", "approve", "reject", "publish"):
            with self.subTest(action=action):
                getattr(self.pipeline, action).side_effect = RuntimeError("offline persistence")
                response = self.review_request(action)
                self.assertEqual(response.status_code, 502, response.text)
                self.assert_lock_available()


if __name__ == "__main__":
    unittest.main()
