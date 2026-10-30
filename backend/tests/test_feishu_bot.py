"""Offline event, worker and chat-only pipeline regressions for Feishu commands."""

import copy
import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repo_maintainer.retrospective_connectors import FeishuConnector, GitHubConnector, MemoryTokenCache
from repo_maintainer.retrospective_feishu_bot import (
    TASK_NAME,
    FeishuIncidentBusy,
    FeishuNotificationError,
    enqueue_feishu_event,
    incident_id_for,
    process_feishu_event,
    review_link,
)
from repo_maintainer.retrospective_feishu_listener import main as listener_main
from repo_maintainer.retrospective_models import (
    Citation,
    EvidenceBackedText,
    EvidenceSource,
    GenerationMetadata,
    ImpactSection,
    IncidentRecord,
    IncidentStatus,
    RetrospectiveDocument,
)
from repo_maintainer.retrospective_pipeline import RetrospectivePipeline
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore


class FakePipeline:
    def __init__(self):
        self.store = InMemoryRetrospectiveStore()
        self.collaboration = SimpleNamespace(reply_text=Mock(return_value={"code": 0}))
        self.run = Mock(side_effect=self._run)

    def _run(self, incident_id):
        self.store.update_incident(incident_id, status=IncidentStatus.PENDING_REVIEW, current_version=1)


class ChatOnlyGenerator:
    """Exercise the real Function Tool loop using only previously collected chat."""

    def __init__(self):
        self.tool_requests = []
        self.generation_inputs = []

    def build_tool_request(self, incident, evidence, history, messages, definitions):
        return {"input": list(messages), "tools": definitions}

    def request_tool_calls(self, request):
        self.tool_requests.append(copy.deepcopy(request))
        if len(self.tool_requests) == 1:
            return {"output": [{"type": "function_call", "call_id": "call-chat", "name": "search_feishu_context",
                                "arguments": json.dumps({"query": "timeout 配置", "limit": 2})}]}
        return {"output": []}

    def generate(self, incident, evidence, history):
        self.generation_inputs.append((incident, evidence, history))
        chat = next(item for item in evidence if item.metadata.get("message_id") == "om-participant")
        citation = Citation(evidence_id=chat.id, quote="timeout 从 1 秒回滚到 10 秒后恢复")
        document = RetrospectiveDocument(
            title=incident.title,
            summary="参与者报告超时配置引发请求失败，回滚后恢复，需人工确认。",
            impact=ImpactSection(scope="参与者报告图像推理请求失败", citations=[citation]),
            root_cause=EvidenceBackedText(content="参与者认为 timeout 配置过低，尚待人工核对。", citations=[citation]),
            fix_solution=EvidenceBackedText(content="参与者报告回滚 timeout 后恢复。", citations=[citation]),
            prevention=["补充配置变更校验与超时监控"],
            related_modules=["image-inference"], keywords=["timeout", "配置回滚"],
        )
        return document, GenerationMetadata(model="offline-chat-generator")


class FeishuBotTests(unittest.TestCase):
    def setUp(self):
        # This suite must never make a real HTTP request, even if a mock is missed.
        blocker = patch("repo_maintainer.retrospective_connectors.urlopen", side_effect=AssertionError("Network disabled in test"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.message_time = int(datetime.now(timezone.utc).timestamp()) - 120
        self.payload = {
            "schema": "2.0",
            "header": {"app_id": "cli-test", "event_type": "im.message.receive_v1", "event_id": "ev-1"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-user"}},
                "message": {
                    "message_id": "om-command", "chat_id": "oc-allowed", "chat_type": "group",
                    "message_type": "text", "create_time": str(self.message_time * 1000),
                    "content": json.dumps({"text": "@_user_1 复盘 配置超时回滚\n服务：image-inference\n环境：prod"}),
                    "mentions": [{"key": "@_user_1", "id": {"open_id": "ou-bot"}}],
                },
            },
        }
        self.scope = dict(bot_open_id="ou-bot", app_id="cli-test", allowed_chat_ids={"oc-allowed"})
        self.identity = incident_id_for("cli-test", "oc-allowed", "om-command")
        self.pipeline = FakePipeline()

    def payload_with_text(self, text):
        payload = copy.deepcopy(self.payload)
        payload["event"]["message"]["content"] = json.dumps({"text": text})
        return payload

    def process(self, payload=None, pipeline=None, **kwargs):
        args = {**self.scope, "job_id": "job-1", "public_base_url": "https://review.test"}
        args.update(kwargs)
        return process_feishu_event(self.payload if payload is None else payload,
                                    pipeline=self.pipeline if pipeline is None else pipeline, **args)

    def test_callback_ignores_unrelated_events_and_publishes_valid_command_once(self):
        queue = Mock()
        queue.send_task.return_value = SimpleNamespace(id="task-1")
        ignored = self.payload_with_text("@_user_1 普通聊天")
        self.assertIsNone(enqueue_feishu_event(ignored, queue=queue, **self.scope))
        queue.send_task.assert_not_called()
        self.assertEqual(enqueue_feishu_event(self.payload, queue=queue, **self.scope), "task-1")
        queue.send_task.assert_called_once_with(TASK_NAME, args=[self.payload, "ou-bot"], retry=False)

    def test_broker_failure_propagates_instead_of_acknowledging_a_lost_command(self):
        queue = Mock()
        queue.send_task.side_effect = ConnectionError("offline broker")
        with self.assertRaisesRegex(ConnectionError, "offline broker"):
            enqueue_feishu_event(self.payload, queue=queue, **self.scope)

    def test_worker_ignores_invalid_scope_without_reply_or_incident(self):
        payload = copy.deepcopy(self.payload)
        payload["event"]["message"]["chat_id"] = "oc-other"
        self.assertEqual(self.process(payload), {"status": "ignored"})
        self.pipeline.collaboration.reply_text.assert_not_called()
        self.pipeline.run.assert_not_called()
        self.assertEqual(self.pipeline.store.list_incidents(), [])

    def test_help_and_invalid_commands_reply_without_creating_incidents(self):
        for text, expected in (("@_user_1 复盘帮助", "help"), ("@_user_1 复盘", "invalid"),
                               ("@_user_1 复盘 配置事故\n仓库：foreign/repo", "invalid")):
            with self.subTest(text=text):
                pipeline = FakePipeline()
                self.assertEqual(self.process(self.payload_with_text(text), pipeline=pipeline), {"status": expected})
                pipeline.run.assert_not_called()
                self.assertEqual(pipeline.store.list_incidents(), [])
                args = pipeline.collaboration.reply_text.call_args.args
                self.assertEqual(args[0], "om-command")
                self.assertIn("72 小时", args[1])
                self.assertTrue(args[2])

    def test_help_redelivery_uses_stable_reply_uuid(self):
        payload = self.payload_with_text("@_user_1 复盘帮助")
        self.process(payload)
        payload["header"]["event_id"] = "ev-redelivery"
        self.process(payload, job_id="job-2")
        calls = self.pipeline.collaboration.reply_text.call_args_list
        self.assertEqual(calls[0].args[2], calls[1].args[2])

    def test_normal_command_creates_current_group_only_and_finishes_pending_review(self):
        result = self.process()
        self.assertEqual(result, {"status": "pending_review", "incident_id": self.identity})
        incident = self.pipeline.store.get_incident(self.identity)
        self.assertEqual(incident.status, IncidentStatus.PENDING_REVIEW)
        self.assertEqual(incident.feishu_chat_ids, ["oc-allowed"])
        self.assertEqual(incident.feishu_doc_tokens, [])
        self.assertEqual(incident.repo_id, "feishu:cli-test:oc-allowed")
        self.assertEqual((incident.repo_name, incident.commit_sha, incident.pr_number), ("", "", None))
        self.assertEqual(incident.source_type, "feishu_message")
        self.assertEqual(incident.metadata["feishu_window"], {"start_time": self.message_time - 7200,
                                                            "end_time": self.message_time})
        self.assertEqual(incident.metadata["service"], "image-inference")
        self.assertEqual(incident.metadata["environment"], "prod")
        self.assertIn("@_user_1", incident.metadata["feishu_trigger_text"])
        self.assertEqual(set(incident.metadata["feishu_notifications"]), {"accepted", "completed"})
        self.pipeline.run.assert_called_once_with(self.identity)
        replies = self.pipeline.collaboration.reply_text.call_args_list
        self.assertEqual(len(replies), 2)
        self.assertIn("当前群", replies[0].args[1])
        self.assertIn("人工审核", replies[0].args[1])
        self.assertIn("incident_id=" + self.identity, replies[1].args[1])
        self.assertEqual(self.pipeline.store._jobs["job-1"]["status"], "completed")

    def test_same_message_new_event_id_does_not_regenerate_or_repeat_saved_notifications(self):
        first = self.process()
        retry = copy.deepcopy(self.payload)
        retry["header"]["event_id"] = "ev-different"
        second = self.process(retry, job_id="job-2")
        self.assertEqual(first, second)
        self.assertEqual(len(self.pipeline.store.list_incidents()), 1)
        self.pipeline.run.assert_called_once()
        self.assertEqual(self.pipeline.collaboration.reply_text.call_count, 2)
        self.assertEqual(self.pipeline.store.get_incident(self.identity).metadata["feishu_event_id"], "ev-1")
        self.assertEqual(self.pipeline.store._jobs["job-2"]["incident_id"], self.identity)

    def test_distinct_messages_in_same_group_create_separate_incidents_with_same_history_scope(self):
        first = self.process()
        second_payload = copy.deepcopy(self.payload)
        second_payload["event"]["message"]["message_id"] = "om-second"
        second = self.process(second_payload, job_id="job-2")
        self.assertNotEqual(first["incident_id"], second["incident_id"])
        incidents = self.pipeline.store.list_incidents()
        self.assertEqual(len(incidents), 2)
        self.assertEqual({item.repo_id for item in incidents}, {"feishu:cli-test:oc-allowed"})
        self.assertEqual(self.pipeline.run.call_count, 2)

    def test_incident_identity_is_bound_to_app_group_and_message(self):
        self.assertEqual(self.identity, incident_id_for("cli-test", "oc-allowed", "om-command"))
        for app, chat, message in (("cli-other", "oc-allowed", "om-command"),
                                   ("cli-test", "oc-other", "om-command"),
                                   ("cli-test", "oc-allowed", "om-other")):
            self.assertNotEqual(self.identity, incident_id_for(app, chat, message))

    def test_generation_failure_is_saved_and_redelivery_does_not_retry_generation(self):
        self.pipeline.run.side_effect = RuntimeError("private-provider-detail")
        with self.assertLogs("repo_maintainer.retrospective_feishu_bot", level="ERROR"):
            result = self.process()
        self.assertEqual(result["status"], "failed")
        incident = self.pipeline.store.get_incident(self.identity)
        self.assertEqual(incident.status, IncidentStatus.FAILED)
        self.assertNotIn("private-provider-detail", incident.error)
        failed_reply = self.pipeline.collaboration.reply_text.call_args.args[1]
        self.assertNotIn("private-provider-detail", failed_reply)
        self.assertIn("查看原因", failed_reply)
        self.process(job_id="job-redelivery")
        self.pipeline.run.assert_called_once()
        self.assertEqual(self.pipeline.collaboration.reply_text.call_count, 2)
        self.assertEqual(self.pipeline.store._jobs["job-redelivery"]["status"], "failed")

    def test_pipeline_recorded_failure_is_preserved_while_reply_omits_provider_details(self):
        def fail(incident_id):
            self.pipeline.store.update_incident(incident_id, status=IncidentStatus.FAILED,
                                                 failed_stage="generating", error="private-model-error")
            raise RuntimeError("private-model-error")

        self.pipeline.run.side_effect = fail
        with self.assertLogs("repo_maintainer.retrospective_feishu_bot", level="ERROR"):
            self.process()
        incident = self.pipeline.store.get_incident(self.identity)
        self.assertEqual((incident.failed_stage, incident.error), ("generating", "private-model-error"))
        self.assertNotIn("private-model-error", self.pipeline.collaboration.reply_text.call_args.args[1])

    def test_completion_notification_retry_does_not_regenerate_and_reuses_uuid(self):
        reply = self.pipeline.collaboration.reply_text
        reply.side_effect = [None, RuntimeError("private-http-error"), None]
        with self.assertRaises(FeishuNotificationError) as caught:
            self.process()
        self.assertNotIn("private-http-error", str(caught.exception))
        incident = self.pipeline.store.get_incident(self.identity)
        self.assertEqual(incident.status, IncidentStatus.PENDING_REVIEW)
        self.assertEqual(incident.metadata["feishu_notifications"], ["accepted"])
        result = self.process(job_id="job-retry")
        self.assertEqual(result["status"], "pending_review")
        self.pipeline.run.assert_called_once()
        self.assertEqual(reply.call_count, 3)
        self.assertEqual(reply.call_args_list[1].args[2], reply.call_args_list[2].args[2])
        self.assertNotEqual(reply.call_args_list[0].args[2], reply.call_args_list[1].args[2])
        self.assertEqual(self.pipeline.store.get_incident(self.identity).metadata["feishu_notifications"],
                         ["accepted", "completed"])

    def test_acceptance_notification_failure_does_not_prevent_generation(self):
        self.pipeline.collaboration.reply_text.side_effect = [RuntimeError("offline"), None]
        with self.assertLogs("repo_maintainer.retrospective_feishu_bot", level="WARNING"):
            result = self.process()
        self.assertEqual(result["status"], "pending_review")
        self.pipeline.run.assert_called_once()
        self.assertEqual(self.pipeline.store.get_incident(self.identity).metadata["feishu_notifications"], ["completed"])

    def test_failed_notification_retry_does_not_retry_failed_generation(self):
        self.pipeline.run.side_effect = RuntimeError("provider offline")
        self.pipeline.collaboration.reply_text.side_effect = [None, RuntimeError("reply offline"), None]
        with self.assertLogs("repo_maintainer.retrospective_feishu_bot", level="ERROR"):
            with self.assertRaises(FeishuNotificationError):
                self.process()
        self.assertEqual(self.process(job_id="job-retry")["status"], "failed")
        self.pipeline.run.assert_called_once()
        calls = self.pipeline.collaboration.reply_text.call_args_list
        self.assertEqual(calls[1].args[2], calls[2].args[2])

    def test_busy_lock_retries_without_creating_or_running_another_incident(self):
        with self.pipeline.store.execution_lock(self.identity) as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(FeishuIncidentBusy):
                self.process()
        self.assertEqual(self.pipeline.store.list_incidents(), [])
        self.pipeline.run.assert_not_called()
        self.pipeline.collaboration.reply_text.assert_not_called()
        self.assertEqual(self.process()["status"], "pending_review")

    def test_interrupted_process_is_reported_failed_instead_of_duplicating_generation(self):
        def crash(incident_id):
            self.pipeline.store.update_incident(incident_id, status=IncidentStatus.GENERATING)
            raise SystemExit("simulated worker death")

        self.pipeline.run.side_effect = crash
        with self.assertRaises(SystemExit):
            self.process()
        self.assertEqual(self.pipeline.store.get_incident(self.identity).status, IncidentStatus.GENERATING)
        result = self.process(job_id="job-redelivery")
        self.assertEqual(result["status"], "failed")
        self.pipeline.run.assert_called_once()
        self.assertIn("执行中断", self.pipeline.store.get_incident(self.identity).error)

    def test_all_in_progress_states_after_lock_release_require_human_retry(self):
        for state in (IncidentStatus.COLLECTING, IncidentStatus.RETRIEVING,
                      IncidentStatus.GENERATING, IncidentStatus.VALIDATING):
            with self.subTest(state=state):
                pipeline = FakePipeline()
                pipeline.store.create_incident(IncidentRecord(id=self.identity, title="interrupted", status=state))
                self.assertEqual(self.process(pipeline=pipeline)["status"], "failed")
                pipeline.run.assert_not_called()
                self.assertEqual(pipeline.store.get_incident(self.identity).status, IncidentStatus.FAILED)

    def test_review_link_supports_subpath_and_urlencodes_identity(self):
        self.assertEqual(review_link("https://review.test/base/", "id +/?"),
                         "https://review.test/base/retrospective?incident_id=id+%2B%2F%3F")
        self.assertIn("事故 ID", review_link("", "incident"))
        for value in ("javascript:alert(1)", "//review.test", "ftp://review.test", "https://user:secret@review.test",
                      "https://review.test?access=token", "https://review.test/#fragment", "https:///path"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    review_link(value, "incident")

    def test_invalid_review_url_fails_before_creating_incident(self):
        with self.assertRaises(ValueError):
            self.process(public_base_url="https://user:secret@review.test")
        self.assertEqual(self.pipeline.store.list_incidents(), [])
        self.pipeline.collaboration.reply_text.assert_not_called()

    def test_real_pipeline_generates_chat_only_review_with_tools_without_pr_or_default_documents(self):
        store = InMemoryRetrospectiveStore()
        store.search_knowledge = Mock(wraps=store.search_knowledge)
        git = GitHubConnector(token="offline-test-token")
        git._json = Mock(side_effect=AssertionError("GitHub must not be queried without a PR"))
        collaboration = FeishuConnector(app_id="cli-test", app_secret="offline-test-secret", cache=MemoryTokenCache())

        def message(message_id, text, sender="ou-user"):
            return {"message_id": message_id, "sender": {"id": sender},
                    "create_time": str((self.message_time - 60) * 1000),
                    "body": {"content": json.dumps({"text": text})}}

        collaboration._list_messages = Mock(return_value=[
            message("om-participant", "参与者报告：图像推理失败，timeout 从 1 秒回滚到 10 秒后恢复。"),
            message("om-command", "duplicate trigger command"),
            message("om-reply", "已受理的机器人回复不是事故原始证据", sender="ou-bot"),
        ])
        collaboration._read_document = Mock(side_effect=AssertionError("Default documents must not be inherited"))
        collaboration.reply_text = Mock(return_value={"code": 0})
        collaboration.publish = Mock(side_effect=AssertionError("Human approval is required before publication"))
        generator = ChatOnlyGenerator()
        embedder = SimpleNamespace(embed_texts=Mock(side_effect=lambda texts: [[1.0, float(len(text)), 0.5] for text in texts]))
        code_context = Mock()
        pipeline = RetrospectivePipeline(
            store=store, git=git, collaboration=collaboration, generator=generator, embedder=embedder,
            code_context=code_context, default_feishu_chat_ids=["oc-unrelated"],
            default_feishu_doc_tokens=["doc-unrelated"],
        )
        result = self.process(pipeline=pipeline)
        self.assertEqual(result["status"], "pending_review")
        incident = store.get_incident(self.identity)
        self.assertEqual(incident.feishu_chat_ids, ["oc-allowed"])
        self.assertEqual(incident.feishu_doc_tokens, [])
        collaboration._list_messages.assert_called_once_with("oc-allowed", self.message_time - 7200, self.message_time)
        collaboration._read_document.assert_not_called()
        collaboration.publish.assert_not_called()
        git._json.assert_not_called()
        code_context.collect.assert_not_called()
        code_context.open_session.assert_not_called()
        self.assertEqual(len(generator.generation_inputs), 1)
        self.assertEqual(len(generator.tool_requests), 2)
        evidence = store.list_evidence(self.identity)
        self.assertEqual({item.source_type for item in evidence}, {EvidenceSource.FEISHU_MESSAGE})
        self.assertEqual({item.metadata["message_id"] for item in evidence}, {"om-command", "om-participant"})
        self.assertEqual(len(store._evidence_embeddings), 2)
        version = store.latest_version(self.identity)
        self.assertEqual(version.version, 1)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(version.quality.citation_accuracy, 1.0)
        self.assertEqual(store.list_review_actions(self.identity), [])
        self.assertIsNone(store.get_publication(version.id))
        for call in store.search_knowledge.call_args_list:
            self.assertEqual(call.kwargs["repo_id"], "feishu:cli-test:oc-allowed")
        tool_results = [context.payload for context in store.list_generation_contexts(self.identity)
                        if context.payload["purpose"] == "tool_execution"]
        self.assertEqual(tool_results[0]["request"]["name"], "search_feishu_context")
        self.assertEqual(tool_results[0]["request"]["result"]["status"], "ok")
        self.process(pipeline=pipeline, job_id="job-redelivered")
        self.assertEqual(len(generator.generation_inputs), 1)
        self.assertEqual(len(store.list_versions(self.identity)), 1)

    def wire_mock_listener(self):
        queue = Mock()
        queue.send_task.return_value = SimpleNamespace(id="sdk-task")
        connector = SimpleNamespace(bot_open_id=Mock(return_value="ou-bot"))
        callbacks = []
        builder = Mock()
        builder.register_p2_im_message_receive_v1.side_effect = lambda callback: callbacks.append(callback) or builder
        fake_lark = SimpleNamespace(
            JSON=SimpleNamespace(marshal=Mock(side_effect=json.dumps)),
            EventDispatcherHandler=SimpleNamespace(builder=Mock(return_value=builder)),
            ws=SimpleNamespace(Client=Mock()), LogLevel=SimpleNamespace(WARNING="warning"),
        )
        fake_celery = SimpleNamespace(Celery=Mock(return_value=queue))
        environment = {"FEISHU_APP_ID": "cli-test", "FEISHU_APP_SECRET": "offline-test-secret",
                       "REDIS_URL": "redis://offline.invalid/0", "FEISHU_CHAT_IDS": "oc-allowed"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.dict("sys.modules", {"lark_oapi": fake_lark, "celery": fake_celery}), \
                patch("repo_maintainer.retrospective_feishu_listener.logging.basicConfig"), \
                patch("repo_maintainer.retrospective_feishu_listener.RedisTokenCache", return_value=object()), \
                patch("repo_maintainer.retrospective_feishu_listener.FeishuConnector", return_value=connector):
            listener_main()
        self.assertEqual(len(callbacks), 1)
        fake_lark.ws.Client.return_value.start.assert_called_once()
        self.assertEqual(fake_lark.ws.Client.call_args.kwargs["log_level"], "warning")
        queue.conf.update.assert_called_once()
        self.assertFalse(queue.conf.update.call_args.kwargs["task_publish_retry"])
        return callbacks[0], queue

    def test_official_sdk_callback_wiring_preserves_filtering_and_broker_failure(self):
        callback, queue = self.wire_mock_listener()
        callback(self.payload_with_text("@_user_1 普通聊天"))
        queue.send_task.assert_not_called()
        callback(self.payload)
        queue.send_task.assert_called_once_with(TASK_NAME, args=[self.payload, "ou-bot"], retry=False)
        queue.send_task.side_effect = ConnectionError("offline broker")
        with self.assertRaises(ConnectionError):
            callback(self.payload)


if __name__ == "__main__":
    unittest.main()
