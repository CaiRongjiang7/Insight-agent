import copy
import json
import unittest
from datetime import datetime, timezone

from repo_maintainer.retrospective_feishu_commands import (
    COMMAND_TIMEZONE,
    DEFAULT_WINDOW_SECONDS,
    FEISHU_COMMAND_HELP,
    parse_feishu_command,
)


class FeishuCommandTests(unittest.TestCase):
    def setUp(self):
        self.sent_at = datetime(2026, 10, 29, 16, 30, 20, tzinfo=COMMAND_TIMEZONE)
        self.now = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
        self.payload = {
            "schema": "2.0",
            "header": {"event_id": "ev-1", "event_type": "im.message.receive_v1", "app_id": "cli-test"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-user"}},
                "message": {
                    "message_id": "om-1", "chat_id": "oc-allowed", "chat_type": "group",
                    "message_type": "text", "create_time": str(int(self.sent_at.timestamp() * 1000)),
                    "content": json.dumps({"text": "@_user_1 发起复盘 图像服务掉线"}),
                    "mentions": [{"key": "@_user_1", "id": {"open_id": "ou-bot"}, "name": "复盘机器人"}],
                },
            },
        }

    def parse(self, text=None, payload=None, **kwargs):
        event = copy.deepcopy(self.payload if payload is None else payload)
        if text is not None:
            event["event"]["message"]["content"] = json.dumps({"text": text})
        config = dict(app_id="cli-test", bot_open_id="ou-bot", allowed_chat_ids={"oc-allowed"}, now=self.now)
        config.update(kwargs)
        return parse_feishu_command(event, **config)

    def test_minimum_command_and_message_anchored_default_window(self):
        result = self.parse()
        self.assertEqual(result.action, "start")
        self.assertEqual(result.title, "图像服务掉线")
        self.assertEqual(result.description, result.title)
        self.assertEqual(result.message_time, int(self.sent_at.timestamp()))
        self.assertEqual(result.end_time, result.message_time)
        self.assertEqual(result.start_time, result.message_time - DEFAULT_WINDOW_SECONDS)
        self.assertEqual((result.message_id, result.chat_id, result.sender_open_id, result.event_id),
                         ("om-1", "oc-allowed", "ou-user", "ev-1"))
        self.assertEqual((result.service, result.environment, result.error), ("", "", ""))

    def test_short_alias_and_all_optional_fields(self):
        result = self.parse("@_user_1 复盘 Nacos 长连接断开\n服务： image-inference\n环境: prod\n时间：2026-10-29 14:00~15:00")
        self.assertEqual(result.action, "start")
        self.assertEqual((result.service, result.environment), ("image-inference", "prod"))
        self.assertEqual(result.end_time - result.start_time, 3600)
        self.assertEqual(datetime.fromtimestamp(result.start_time, COMMAND_TIMEZONE).hour, 14)
        self.assertIn("服务： image-inference", result.description)
        self.assertNotIn("@_user_1", result.description)

    def test_today_uses_message_date_even_when_delivered_tomorrow(self):
        result = self.parse("@_user_1 复盘 配置回滚\n时间：今天 14:00-15:00")
        self.assertEqual(result.action, "start")
        self.assertEqual(datetime.fromtimestamp(result.start_time, COMMAND_TIMEZONE).isoformat(),
                         "2026-10-29T14:00:00+08:00")

    def test_explicit_cross_date_and_72_hour_window_are_supported(self):
        for interval in (
            "2026-10-28 23:00~2026-10-29 01:00",
            "2026-10-26 16:00至2026-10-29 16:00",
            "2026-10-28 23:00-2026-10-29 01:00",
            "2026-10-29 8:00～9:00",
        ):
            with self.subTest(interval=interval):
                self.assertEqual(self.parse(f"@_user_1 复盘 事故\n时间：{interval}").action, "start")

    def test_invalid_time_ranges_report_readable_errors(self):
        for interval in (
            "2026-10-28 23:00~01:00",  # Never infer next day.
            "今天 15:00-14:00",
            "今天 14:00-14:00",
            "今天 14:00-17:00",  # Future relative to send time, although processing is later.
            "2026-10-26 15:59~2026-10-29 16:00",
            "2026-02-30 14:00~15:00",
            "今天 25:00-26:00",
            "今天 14:60-15:00",
            "昨天 14:00-15:00",
            "14:00-15:00",
            "最近三天",
            "2026-10-29T14:00:00Z~2026-10-29T15:00:00Z",
        ):
            with self.subTest(interval=interval):
                result = self.parse(f"@_user_1 复盘 事故\n时间：{interval}")
                self.assertEqual(result.action, "invalid")
                self.assertTrue(result.error)
                self.assertIsNone(result.start_time)

    def test_help_command(self):
        result = self.parse("@_user_1 复盘帮助")
        self.assertEqual(result.action, "help")
        self.assertIsNone(result.start_time)
        self.assertIn("72 小时", FEISHU_COMMAND_HELP)
        self.assertEqual(self.parse("@_user_1 复盘帮助 多余参数").action, "invalid")

    def test_missing_title_is_invalid_even_when_followed_by_fields(self):
        for text in ("@_user_1 复盘", "@_user_1 发起复盘\n服务：image", "@_user_1 复盘 \n时间：今天 14:00-15:00"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).action, "invalid")

    def test_unknown_duplicate_and_empty_fields_are_invalid(self):
        for fields in (
            "仓库：other/repo", "群：oc-foreign", "文档：token", "SHA: abc", "原因未知",
            "服务：one\n服务：two", "环境：prod\n环境:staging",
            "时间：今天 14:00-15:00\n时间：今天 13:00-14:00",
            "服务：", "环境：  ", "时间：", "服务：" + "x" * 101,
            "环境：" + "x" * 101,
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.parse(f"@_user_1 复盘 配置事故\n{fields}").action, "invalid")

    def test_title_and_message_length_limits(self):
        self.assertEqual(self.parse("@_user_1 复盘 " + "事" * 200).action, "start")
        self.assertEqual(self.parse("@_user_1 复盘 " + "事" * 201).action, "invalid")
        result = self.parse("@_user_1 复盘 事故\n" + " " * 4000)
        self.assertEqual(result.action, "invalid")
        self.assertIn("4000", result.error)

    def test_blank_lines_and_crlf_are_supported(self):
        result = self.parse("@_user_1 复盘 配置事故\r\n\r\n服务：image\r\n环境：prod\r\n")
        self.assertEqual((result.action, result.service, result.environment), ("start", "image", "prod"))

    def test_control_character_is_invalid(self):
        self.assertEqual(self.parse("@_user_1 复盘 事故\x00").action, "invalid")

    def test_unrelated_chats_are_silent_even_when_bot_is_mentioned(self):
        for text in ("@_user_1 你好", "@_user_1 这个事故需要复盘", "@_user_1 复盘一下这个问题", "@_user_1 hello\n复盘 配置问题", "普通聊天" * 5000):
            with self.subTest(text=text[:30]):
                self.assertIsNone(self.parse(text))

    def test_scope_sender_and_event_type_are_required(self):
        cases = (
            (("header", "event_type"), "im.chat.member.user.added_v1"),
            (("header", "app_id"), "cli-foreign"),
            (("event", "sender", "sender_type"), "app"),
            (("event", "sender", "sender_type"), "bot"),
            (("event", "message", "chat_type"), "p2p"),
            (("event", "message", "chat_id"), "oc-foreign"),
            (("event", "message", "message_type"), "post"),
            (("event", "message", "message_id"), ""),
            (("event", "sender", "sender_id", "open_id"), ""),
            (("header", "event_id"), ""),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value):
                payload = copy.deepcopy(self.payload)
                current = payload
                for segment in path[:-1]:
                    current = current[segment]
                current[path[-1]] = value
                self.assertIsNone(self.parse(payload=payload))

    def test_own_mention_must_exist_in_metadata_and_actual_text(self):
        self.assertIsNone(self.parse("发起复盘 没有真正提及"))
        self.assertIsNone(self.parse("@_user_10 发起复盘 key前缀碰撞"))
        self.assertIsNone(self.parse("x@_user_1 发起复盘 不是独立提及"))
        for mentions in ([], None, {}, [{"key": "@_user_1", "id": {"open_id": "ou-other"}}],
                         [{"key": "", "id": {"open_id": "ou-bot"}}]):
            with self.subTest(mentions=mentions):
                payload = copy.deepcopy(self.payload)
                payload["event"]["message"]["mentions"] = mentions
                self.assertIsNone(self.parse(payload=payload))

    def test_mentions_cannot_supply_a_command_or_forge_bot_identity_by_name(self):
        payload = copy.deepcopy(self.payload)
        payload["event"]["message"]["mentions"] = [{"key": "复盘", "id": {"open_id": "ou-bot"}}]
        self.assertIsNone(self.parse("复盘 发起复盘 标题", payload=payload))
        payload["event"]["message"]["mentions"] = [{"key": "@_user_1", "name": "复盘机器人", "id": {"open_id": "ou-other"}}]
        self.assertIsNone(self.parse(payload=payload))

    def test_wrong_or_empty_configuration_is_silent(self):
        for config in ({"app_id": ""}, {"bot_open_id": ""}, {"allowed_chat_ids": set()},
                       {"allowed_chat_ids": "oc-allowed"}, {"bot_open_id": "ou-other"}):
            with self.subTest(config=config):
                self.assertIsNone(self.parse(**config))

    def test_malformed_envelopes_do_not_raise(self):
        for payload in ({}, {"header": []}, {"header": self.payload["header"], "event": None},
                        [], "not a mapping", None):
            with self.subTest(payload=payload):
                result = parse_feishu_command(payload, app_id="cli-test", bot_open_id="ou-bot",
                                              allowed_chat_ids={"oc-allowed"}, now=self.now)
                self.assertIsNone(result)
        for content in (None, {}, "{bad", "[]", '"string"', '{"text": null}', '{"text": 10}'):
            with self.subTest(content=content):
                payload = copy.deepcopy(self.payload)
                payload["event"]["message"]["content"] = content
                self.assertIsNone(self.parse(payload=payload))

    def test_bad_message_timestamp_or_future_event_is_ignored(self):
        for timestamp in (None, "", "nan", "0", "-1", True, "9" * 30,
                          "１２３４５６", str(int((self.now.timestamp() + 301) * 1000))):
            with self.subTest(timestamp=timestamp):
                payload = copy.deepcopy(self.payload)
                payload["event"]["message"]["create_time"] = timestamp
                self.assertIsNone(self.parse(payload=payload))


if __name__ == "__main__":
    unittest.main()
