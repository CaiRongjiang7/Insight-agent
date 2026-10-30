"""Offline checks for Feishu trigger identity, replies, windows and evidence origin."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from repo_maintainer.retrospective_connectors import (
    FeishuConnector,
    GitHubConnector,
    MemoryTokenCache,
    _incident_time_window,
    _manual_incident_evidence,
)
from repo_maintainer.retrospective_models import EvidenceSource, IncidentRecord


class FeishuConnectorTests(unittest.TestCase):
    def setUp(self):
        self.connector = FeishuConnector(app_id="test-app", app_secret="test-secret", cache=MemoryTokenCache())
        self.transport = patch("repo_maintainer.retrospective_connectors._http_json")
        self.http = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.connector.cache.set("feishu:tenant_token:test-app", "test-tenant-token", 60)

    def test_bot_open_id_reads_top_level_bot(self):
        self.http.return_value = {"code": 0, "bot": {"open_id": "ou_own_bot"}}
        self.assertEqual(self.connector.bot_open_id(), "ou_own_bot")
        args = self.http.call_args
        self.assertEqual(args.args[:2], ("GET", "https://open.feishu.cn/open-apis/bot/v3/info"))
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bearer test-tenant-token")

    def test_bot_open_id_rejects_missing_or_malformed_identity(self):
        for response in ({"data": {"bot": {"open_id": "wrong-shape"}}}, {"bot": {}},
                         {"bot": None}, {"bot": {"open_id": " "}}, {"bot": {"open_id": 123}}):
            with self.subTest(response=response):
                self.http.return_value = response
                with self.assertRaisesRegex(RuntimeError, "bot.open_id"):
                    self.connector.bot_open_id()

    def test_reply_uses_json_text_content_and_caller_uuid(self):
        self.http.return_value = {"code": 0, "data": {"message_id": "om_reply"}}
        result = self.connector.reply_text("om_source", "复盘已创建\n正在收集证据", "stable-reply-uuid")
        args = self.http.call_args.args
        self.assertEqual(args[:2], ("POST", "https://open.feishu.cn/open-apis/im/v1/messages/om_source/reply"))
        self.assertEqual(args[2]["msg_type"], "text")
        self.assertIsInstance(args[2]["content"], str)
        self.assertEqual(json.loads(args[2]["content"]), {"text": "复盘已创建\n正在收集证据"})
        self.assertEqual(args[2]["uuid"], "stable-reply-uuid")
        self.assertEqual(result["data"]["message_id"], "om_reply")

    def test_collect_uses_window_and_excludes_own_bot_and_duplicate_trigger(self):
        incident = IncidentRecord(
            title="配置复盘", source_type="feishu_message", feishu_chat_ids=["oc_test"],
            metadata={"feishu_window": {"start_time": 100, "end_time": 200},
                      "feishu_bot_open_id": "ou_bot", "feishu_message_id": "om_trigger"},
        )
        messages = [
            {"message_id": "om_trigger", "sender": {"id": "ou_user"}, "body": {"content": '{"text":"原始命令"}'}},
            {"message_id": "om_bot", "sender": {"id": "ou_bot"}, "body": {"content": '{"text":"自动生成的复盘"}'}},
            {"message_id": "om_bot_app", "sender": {"id": "test-app", "id_type": "app_id", "sender_type": "app"},
             "body": {"content": '{"text":"上次自动复盘回复"}'}},
            {"message_id": "om_human", "sender": {"id": "ou_user"}, "create_time": "150000",
             "body": {"content": '{"text":"已回滚配置"}'}},
            {"message_id": "om_other_bot", "sender": {"id": "monitor-app", "id_type": "app_id", "sender_type": "app"}, "create_time": "160000",
             "body": {"content": '{"text":"服务错误率恢复"}'}},
        ]
        with patch.object(self.connector, "_list_messages", return_value=messages) as listing:
            evidence = self.connector.collect(incident)
        listing.assert_called_once_with("oc_test", 100, 200)
        self.assertEqual([item.metadata["message_id"] for item in evidence], ["om_human", "om_other_bot"])
        self.assertTrue(all(item.source_type == EvidenceSource.FEISHU_MESSAGE for item in evidence))
        self.http.assert_not_called()

    def test_collect_without_bot_metadata_keeps_human_messages(self):
        incident = IncidentRecord(title="手工复盘", feishu_chat_ids=["oc_test"])
        with patch.object(self.connector, "_list_messages", return_value=[
            {"message_id": "om_human", "body": {"content": '{"text":"排查记录"}'}},
        ]):
            evidence = self.connector.collect(incident)
        self.assertEqual(len(evidence), 1)


class FeishuWindowTests(unittest.TestCase):
    def test_explicit_window_including_72_hour_boundary(self):
        incident = IncidentRecord(title="test", metadata={
            "feishu_window": {"start_time": 100, "end_time": 100 + 72 * 3600},
            "occurred_at": "2026-10-29T12:00:00+00:00",
        })
        self.assertEqual(_incident_time_window(incident), (100, 100 + 72 * 3600))

    def test_rejects_malformed_or_unbounded_explicit_windows(self):
        windows = [None, [], {}, {"start_time": 1}, {"start_time": "1", "end_time": 2},
                   {"start_time": True, "end_time": 2}, {"start_time": 1.0, "end_time": 2},
                   {"start_time": 1, "end_time": "2"}, {"start_time": 2, "end_time": 2},
                   {"start_time": 3, "end_time": 2}, {"start_time": 0, "end_time": 72 * 3600 + 1}]
        for window in windows:
            with self.subTest(window=window):
                with self.assertRaisesRegex(ValueError, "feishu_window"):
                    _incident_time_window(IncidentRecord(title="test", metadata={"feishu_window": window}))

    def test_legacy_window_remains_three_days_before_one_after(self):
        center = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
        for key in ("occurred_at", "merged_at"):
            with self.subTest(key=key):
                incident = IncidentRecord(title="test", metadata={key: center.isoformat()})
                self.assertEqual(_incident_time_window(incident), (
                    int((center - timedelta(days=3)).timestamp()), int((center + timedelta(days=1)).timestamp()),
                ))


class TriggerEvidenceTests(unittest.TestCase):
    def test_feishu_trigger_preserves_original_and_provenance(self):
        raw = "@_user_1 复盘 配置回滚\n实际恢复步骤：把超时改回 30s。 "
        occurred_at = "2026-10-29T11:30:00+00:00"
        incident = IncidentRecord(
            title="提炼后的标题", description="解析后描述不应代替消息原文", source_type="feishu_message",
            metadata={"feishu_trigger_text": raw, "feishu_message_id": "om_trigger",
                      "feishu_chat_id": "oc_source", "occurred_at": occurred_at},
        )
        # Initial evidence also reaches this helper through the no-PR GitHub path.
        evidence = GitHubConnector(token="").collect(incident)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].source_type, EvidenceSource.FEISHU_MESSAGE)
        self.assertEqual(evidence[0].content, raw)
        self.assertEqual(evidence[0].metadata, {"message_id": "om_trigger", "chat_id": "oc_source"})
        self.assertEqual(evidence[0].occurred_at, occurred_at)
        self.assertEqual(evidence[0].source_url, "feishu://message/om_trigger")
        self.assertIn("oc_source", evidence[0].location)
        self.assertIn("om_trigger", evidence[0].location)

    def test_missing_trigger_original_does_not_fabricate_message(self):
        evidence = _manual_incident_evidence(IncidentRecord(title="仅标题", source_type="feishu_message"))
        self.assertEqual(evidence, [])

    def test_non_github_sources_are_manual_not_pr_evidence(self):
        for source in ("manual", "config_change", "alert", "github_pr"):
            with self.subTest(source=source):
                evidence = _manual_incident_evidence(IncidentRecord(title="标题", description="描述", source_type=source))
                expected = EvidenceSource.GITHUB_PR if source == "github_pr" else EvidenceSource.MANUAL
                self.assertEqual(evidence[0].source_type, expected)
                self.assertEqual(evidence[0].content, "标题\n\n描述")


if __name__ == "__main__":
    unittest.main()
