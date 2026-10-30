"""Offline compatibility checks using the real Feishu SDK and Celery task runner."""

from __future__ import annotations

import importlib
import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from psycopg import InterfaceError, OperationalError

from repo_maintainer.retrospective_feishu_bot import FeishuIncidentBusy, incident_id_for
from repo_maintainer.retrospective_feishu_commands import parse_feishu_command
from repo_maintainer.retrospective_models import IncidentRecord, IncidentStatus
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore


def command_payload() -> dict:
    sent_at = int(datetime.now(timezone.utc).timestamp()) - 120
    return {
        "schema": "2.0",
        "header": {
            "event_id": "ev-runtime", "event_type": "im.message.receive_v1",
            "app_id": "cli-runtime", "tenant_key": "tenant-offline",
            "create_time": str(sent_at * 1000),
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-user"}},
            "message": {
                "message_id": "om-runtime", "chat_id": "oc-runtime", "chat_type": "group",
                "message_type": "text", "create_time": str(sent_at * 1000),
                "content": json.dumps({"text": "@_user_1 发起复盘 配置超时回滚\n服务：image-inference\n环境：prod"}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou-bot"}, "name": "复盘机器人"}],
            },
        },
    }


class OfflinePipeline:
    """Keep real incident persistence while replacing remote generation and replies."""

    def __init__(self):
        self.store = InMemoryRetrospectiveStore()
        self.collaboration = SimpleNamespace(reply_text=Mock(return_value={"code": 0}))
        self.run = Mock(side_effect=self._run)

    def _run(self, incident_id):
        incident = self.store.get_incident(incident_id)
        self.store.update_incident(
            incident_id, status=IncidentStatus.PENDING_REVIEW,
            current_version=incident.current_version + 1,
        )
        return SimpleNamespace(model_dump=lambda **kwargs: {"incident_id": incident_id, "version": 1})


class OfficialFeishuSdkTests(unittest.TestCase):
    def test_real_event_model_roundtrip_and_dispatch_preserve_command_fields(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        payload = command_payload()
        data = P2ImMessageReceiveV1(payload)
        self.assertEqual(data.event.message.mentions[0].id.open_id, "ou-bot")
        self.assertEqual(data.header.app_id, "cli-runtime")
        restored = json.loads(lark.JSON.marshal(data))
        command = parse_feishu_command(
            restored, app_id="cli-runtime", bot_open_id="ou-bot", allowed_chat_ids={"oc-runtime"},
        )
        self.assertIsNotNone(command)
        self.assertEqual(command.action, "start")
        self.assertEqual(command.message_id, "om-runtime")
        self.assertEqual(command.title, "配置超时回滚")
        self.assertEqual(command.service, "image-inference")
        self.assertEqual(command.environment, "prod")
        self.assertEqual(command.message_time, int(payload["event"]["message"]["create_time"]) // 1000)

        received = []
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(lambda event: received.append(json.loads(lark.JSON.marshal(event))))
            .build()
        )
        # This is the trusted dispatch path used by the official WebSocket client.
        # Do not start a client or perform a handshake in this compatibility test.
        with patch("socket.socket.connect", side_effect=AssertionError("Network disabled in test")):
            handler._do_without_validation(json.dumps(payload).encode("utf-8"))
        self.assertEqual(received, [restored])


class FeishuCeleryRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The worker module reads REDIS_URL at import. Use only in-process transports.
        with patch.dict(os.environ, {"REDIS_URL": "memory://"}):
            cls.tasks = importlib.import_module("repo_maintainer.retrospective_tasks")
        cls.app = cls.tasks.celery_app
        configuration = {
            "broker_url": "memory://", "result_backend": "cache+memory://",
            "task_always_eager": True, "task_eager_propagates": False,
            "task_store_eager_result": False, "task_ignore_result": True,
        }
        cls.previous_configuration = {name: cls.app.conf[name] for name in configuration}
        cls.app.conf.update(configuration)

    @classmethod
    def tearDownClass(cls):
        cls.app.conf.update(cls.previous_configuration)

    def setUp(self):
        self.payload = command_payload()
        self.identity = incident_id_for("cli-runtime", "oc-runtime", "om-runtime")
        self.pipeline = OfflinePipeline()
        for blocker in (
            patch.dict(os.environ, {
                "FEISHU_APP_ID": "cli-runtime", "FEISHU_CHAT_IDS": "oc-runtime",
                "REVIEW_PUBLIC_BASE_URL": "https://review.example.test",
            }),
            patch("socket.socket.connect", side_effect=AssertionError("Network disabled in test")),
            patch("socket.create_connection", side_effect=AssertionError("Network disabled in test")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)
        pipeline_patch = patch.object(self.tasks, "create_pipeline_from_env", return_value=self.pipeline)
        self.factory = pipeline_patch.start()
        self.addCleanup(pipeline_patch.stop)

    def apply_feishu(self, task_id):
        # Real Task.apply executes Celery's autoretry wrapper and eager retry signature.
        return self.tasks.handle_feishu_command.apply(
            args=[self.payload, "ou-bot"], task_id=task_id, throw=False,
        )

    def test_database_first_attempt_failure_retries_and_creates_only_one_incident(self):
        for exception in (OperationalError, InterfaceError):
            with self.subTest(exception=exception.__name__):
                self.pipeline = OfflinePipeline()
                self.factory.reset_mock()
                self.factory.return_value = self.pipeline
                original_create = self.pipeline.store.create_incident
                attempts = []

                def transient_create(incident):
                    attempts.append(incident.id)
                    if len(attempts) == 1:
                        raise exception("offline simulated database reconnect")
                    return original_create(incident)

                task = self.tasks.handle_feishu_command
                with patch.object(self.pipeline.store, "create_incident", side_effect=transient_create), \
                        patch.object(task, "retry", wraps=task.retry) as retry:
                    result = self.apply_feishu("database-" + exception.__name__)
                self.assertTrue(result.successful(), str(result.result))
                self.assertEqual(result.result, {"status": "pending_review", "incident_id": self.identity})
                self.assertEqual(retry.call_count, 1)
                self.assertEqual(self.factory.call_count, 2)
                self.assertEqual(attempts, [self.identity, self.identity])
                self.assertEqual(len(self.pipeline.store.list_incidents()), 1)
                self.pipeline.run.assert_called_once_with(self.identity)
                self.assertEqual(self.pipeline.store.get_incident(self.identity).current_version, 1)
                self.assertEqual(self.pipeline.collaboration.reply_text.call_count, 2)

    def test_completion_reply_failure_retries_without_regeneration(self):
        replies = self.pipeline.collaboration.reply_text
        replies.side_effect = [None, RuntimeError("offline simulated reply timeout"), None]
        task = self.tasks.handle_feishu_command
        with patch.object(task, "retry", wraps=task.retry) as retry:
            result = self.apply_feishu("notification-retry")
        self.assertTrue(result.successful(), str(result.result))
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(self.factory.call_count, 2)
        self.pipeline.run.assert_called_once_with(self.identity)
        self.assertEqual(len(self.pipeline.store.list_incidents()), 1)
        incident = self.pipeline.store.get_incident(self.identity)
        self.assertEqual((incident.status, incident.current_version), (IncidentStatus.PENDING_REVIEW, 1))
        self.assertEqual(incident.metadata["feishu_notifications"], ["accepted", "completed"])
        self.assertEqual(replies.call_count, 3)
        self.assertEqual(replies.call_args_list[1].args[2], replies.call_args_list[2].args[2])

    def test_legacy_task_skips_every_state_other_than_received_or_failed(self):
        for status in IncidentStatus:
            if status in {IncidentStatus.RECEIVED, IncidentStatus.FAILED}:
                continue
            with self.subTest(status=status):
                identity = "inc-skip-" + status.value
                incident = self.pipeline.store.create_incident(IncidentRecord(
                    id=identity, title="Existing incident", status=status,
                    current_version=1, metadata={"keep": "original"},
                ))
                result = self.tasks.run_retrospective.apply(
                    args=[identity], task_id="job-skip-" + status.value, throw=False,
                )
                self.assertTrue(result.successful(), str(result.result))
                self.assertEqual(result.result, {"incident_id": identity, "status": "skipped"})
                self.assertEqual(self.pipeline.store.get_incident(identity).model_dump(), incident.model_dump())
                self.assertEqual(self.pipeline.store._jobs["job-skip-" + status.value]["status"], "skipped")
        self.pipeline.run.assert_not_called()

    def test_busy_locks_exhaust_bounded_task_retries_without_changing_incident(self):
        incident = self.pipeline.store.create_incident(IncidentRecord(
            id=self.identity, title="Owner still generating", status=IncidentStatus.GENERATING,
            metadata={"owner": "another-worker"},
        ))
        for task, args in (
            (self.tasks.handle_feishu_command, [self.payload, "ou-bot"]),
            (self.tasks.run_retrospective, [self.identity]),
        ):
            with self.subTest(task=task.name):
                self.factory.reset_mock()
                with self.pipeline.store.execution_lock(self.identity) as acquired:
                    self.assertTrue(acquired)
                    with patch.object(task, "retry", wraps=task.retry) as retry:
                        result = task.apply(args=args, task_id="busy-" + task.name, throw=False)
                self.assertTrue(result.failed())
                self.assertIsInstance(result.result, FeishuIncidentBusy)
                self.assertGreater(retry.call_count, 1)
                self.assertEqual(self.factory.call_count, retry.call_count)
                self.assertEqual(self.pipeline.store.get_incident(self.identity).model_dump(), incident.model_dump())
                self.assertEqual(self.pipeline.store._jobs, {})
        self.pipeline.run.assert_not_called()
        self.pipeline.collaboration.reply_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
