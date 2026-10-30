"""Parse explicitly mentioned Feishu retrospective commands without network or LLM calls."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal


MAX_COMMAND_CHARS = 4000
MAX_TITLE_CHARS = 200
MAX_WINDOW_SECONDS = 72 * 60 * 60
DEFAULT_WINDOW_SECONDS = 24 * 60 * 60
# Commands use current China Standard Time; no host timezone/tzdata dependency.
COMMAND_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
FEISHU_COMMAND_HELP = (
    "请在已配置的群中 @机器人，然后发送：\n"
    "发起复盘 图像推理服务注册掉线\n"
    "服务：image-inference\n"
    "环境：prod\n"
    "时间：2026-10-29 14:00~15:00\n\n"
    "也可使用“复盘 <标题>”；仅标题必填，服务、环境、时间可省略。"
    "时间按北京时间（Asia/Shanghai），支持“今天 14:00-15:00”或"
    "“2026-10-28 23:00~2026-10-29 01:00”。"
    "未填时间默认查询这条消息发送前 24 小时；填写时另起一行“时间：开始日期 时刻~结束日期 时刻”。"
    "同一天可省略结束日期，也可显式指定短于 24 小时的范围；窗口不能超过 72 小时或晚于消息发送时间。"
    "跨日请写出两个日期，不会自动推断次日。标题最多 200 字，整条命令最多 4000 字。"
    "仅支持文本消息及以上三个可选字段，不支持指定仓库、其他群或文档。"
    "发送“@机器人 复盘帮助”可查看此说明。"
)

_COMMAND = re.compile(r"^(复盘帮助|发起复盘|复盘)(?=\s|$)")
_FIELD = re.compile(r"^(服务|环境|时间)\s*[:：]\s*(.*?)\s*$")
_DAY = r"(?:\d{4}-\d{2}-\d{2}|今天)"
_TIME_RANGE = re.compile(
    rf"^(?P<start_day>{_DAY})\s+(?P<start_hour>\d{{1,2}}):(?P<start_minute>\d{{2}})"
    rf"\s*(?:~|～|至|—|-)\s*"
    rf"(?:(?P<end_day>{_DAY})\s+)?(?P<end_hour>\d{{1,2}}):(?P<end_minute>\d{{2}})$"
)


@dataclass(frozen=True)
class ParsedFeishuCommand:
    """An actionable command; all timestamps are Unix seconds, never milliseconds."""

    action: Literal["start", "help", "invalid"]
    message_id: str
    chat_id: str
    sender_open_id: str
    event_id: str
    message_time: int
    title: str = ""
    # The user's title and optional field lines, with the bot mention/verb removed.
    description: str = ""
    start_time: int | None = None
    end_time: int | None = None
    service: str = ""
    environment: str = ""
    error: str = ""


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        return ""
    return value if not any(character.isspace() for character in value) else ""


def _message_timestamp(value: object, now: datetime) -> int | None:
    # The official message.create_time is a decimal millisecond timestamp.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    raw = str(value)
    if not raw.isascii() or not raw.isdigit() or len(raw) > 16:
        return None
    seconds = int(raw) // 1000
    try:
        datetime.fromtimestamp(seconds, COMMAND_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return None
    # Allow small platform/host clock skew, but do not process fabricated future events.
    if seconds <= 0 or seconds > int(now.timestamp()) + 300:
        return None
    return seconds


def _parse_window(value: str, message_time: int) -> tuple[int, int]:
    match = _TIME_RANGE.fullmatch(value)
    if match is None:
        raise ValueError("时间格式无效，请使用“今天 14:00-15:00”或完整日期范围。")
    anchor = datetime.fromtimestamp(message_time, COMMAND_TIMEZONE)

    def endpoint(day: str, hour: str, minute: str) -> int:
        date = anchor.date() if day == "今天" else datetime.strptime(day, "%Y-%m-%d").date()
        return int(datetime(
            date.year, date.month, date.day, int(hour), int(minute), tzinfo=COMMAND_TIMEZONE,
        ).timestamp())

    try:
        start = endpoint(match["start_day"], match["start_hour"], match["start_minute"])
        end = endpoint(match["end_day"] or match["start_day"], match["end_hour"], match["end_minute"])
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("时间包含无效日期或时刻，请检查日期及小时、分钟。") from exc
    if start >= end:
        raise ValueError("结束时间必须晚于开始时间；跨午夜请明确填写结束日期。")
    if end - start > MAX_WINDOW_SECONDS:
        raise ValueError("时间窗口不能超过 72 小时。")
    if end > message_time:
        raise ValueError("结束时间不能晚于这条消息的发送时间。")
    return start, end


def parse_feishu_command(
    payload: dict,
    *,
    app_id: str,
    bot_open_id: str,
    allowed_chat_ids: Collection[str],
    now: datetime | None = None,
) -> ParsedFeishuCommand | None:
    """Return None for irrelevant/untrusted envelopes; validate only explicit commands.

    Transport authentication belongs to the official event listener. This parser
    additionally binds the event to the configured app, bot and group allowlist.
    ``now`` is only a clock-skew guard: relative dates and retrieval windows always
    use the message's send time. A naive test clock is interpreted as UTC.
    """
    if not app_id or not bot_open_id or not allowed_chat_ids or isinstance(allowed_chat_ids, str):
        return None
    header = _mapping(_mapping(payload).get("header"))
    if header.get("event_type") != "im.message.receive_v1" or header.get("app_id") != app_id:
        return None
    event = _mapping(_mapping(payload).get("event"))
    sender = _mapping(event.get("sender"))
    message = _mapping(event.get("message"))
    if sender.get("sender_type") != "user" or message.get("chat_type") != "group":
        return None
    chat_id = _identifier(message.get("chat_id"))
    if not chat_id or chat_id not in allowed_chat_ids or message.get("message_type") != "text":
        return None
    message_id = _identifier(message.get("message_id"))
    sender_open_id = _identifier(_mapping(sender.get("sender_id")).get("open_id"))
    event_id = _identifier(header.get("event_id"))
    if not all((message_id, sender_open_id, event_id)):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        text = _mapping(json.loads(content)).get("text")
    except (ValueError, RecursionError):
        return None
    if not isinstance(text, str):
        return None
    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        return None
    mentioned = False
    command_text = text
    for item in mentions:
        mention = _mapping(item)
        if _mapping(mention.get("id")).get("open_id") != bot_open_id:
            continue
        key = _identifier(mention.get("key"))
        if not key.startswith("@"):
            continue
        # A mention key must be an actual token, not a prefix of @_user_10 or text
        # embedded in an unrelated identifier. Only verified own mentions are removed.
        pattern = re.compile(r"(?<![A-Za-z0-9_@])" + re.escape(key) + r"(?![A-Za-z0-9_])")
        command_text, count = pattern.subn(" ", command_text)
        mentioned = mentioned or bool(count)
    if not mentioned:
        return None
    command_text = command_text.strip()
    command = _COMMAND.match(command_text)
    if command is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    message_time = _message_timestamp(message.get("create_time"), current)
    if message_time is None:
        return None
    routing = dict(message_id=message_id, chat_id=chat_id, sender_open_id=sender_open_id,
                   event_id=event_id, message_time=message_time)

    def invalid(error: str) -> ParsedFeishuCommand:
        return ParsedFeishuCommand(action="invalid", error=error, **routing)

    if len(text) > MAX_COMMAND_CHARS:
        return invalid("整条命令最多 4000 字，请缩短后重试。")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in command_text):
        return invalid("命令含有不支持的控制字符，请使用普通文本。")
    remainder = command_text[command.end():].strip()
    if command[1] == "复盘帮助":
        return invalid("“复盘帮助”后无需填写标题或参数。") if remainder else ParsedFeishuCommand(action="help", **routing)
    # Do not let an optional field on the next line become an accidentally inferred title.
    first_line = command_text.splitlines()[0]
    title = first_line[command.end():].strip()
    if not title:
        return invalid("缺少事故标题，请在“发起复盘”或“复盘”同一行填写标题。")
    if len(title) > MAX_TITLE_CHARS:
        return invalid("事故标题最多 200 字，请缩短后重试。")
    fields: dict[str, str] = {}
    for line in command_text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        field = _FIELD.fullmatch(line)
        if field is None:
            return invalid("存在无法识别的参数行；仅支持“服务：”“环境：”“时间：”。")
        name, value = field.groups()
        if name in fields:
            return invalid(f"“{name}”重复填写，请仅保留一行。")
        if not value:
            return invalid(f"“{name}”不能为空；不需要时请删除这一行。")
        if name in ("服务", "环境") and len(value) > 100:
            return invalid(f"“{name}”最多 100 字。")
        fields[name] = value
    try:
        start, end = _parse_window(fields["时间"], message_time) if "时间" in fields else (
            message_time - DEFAULT_WINDOW_SECONDS, message_time,
        )
    except ValueError as exc:
        return invalid(str(exc))
    return ParsedFeishuCommand(
        action="start", title=title, description=remainder,
        start_time=start, end_time=end,
        service=fields.get("服务", ""), environment=fields.get("环境", ""), **routing,
    )
