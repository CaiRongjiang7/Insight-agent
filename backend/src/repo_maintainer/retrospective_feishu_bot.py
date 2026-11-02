"""Explicit Feishu commands: fast event enqueue, durable incident dedup, worker replies."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .retrospective_feishu_commands import FEISHU_COMMAND_HELP, parse_feishu_command
from .retrospective_models import IncidentRecord, IncidentStatus
from .retrospective_notifications import review_link

logger = logging.getLogger(__name__)
TASK_NAME = "repo_maintainer.handle_feishu_command"


class FeishuNotificationError(RuntimeError):
    """Retry notification delivery without regenerating an existing incident."""


class FeishuIncidentBusy(RuntimeError):
    """Another worker owns this command; retry after it releases the lock."""


def incident_id_for(app_id: str, chat_id: str, message_id: str) -> str:
    identity = json.dumps([app_id, chat_id, message_id], separators=(",", ":"))
    return "inc_fs_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def enqueue_feishu_event(payload: dict, *, queue, app_id: str, bot_open_id: str,
                         allowed_chat_ids) -> str | None:
    """Only durable broker publication occurs in the SDK callback, before its ACK."""
    command = parse_feishu_command(payload, app_id=app_id, bot_open_id=bot_open_id,
                                   allowed_chat_ids=allowed_chat_ids)
    if command is None:
        return None
    # Failure must propagate: the SDK returns a failed ACK and Feishu retries.
    result = queue.send_task(TASK_NAME, args=[payload, bot_open_id], retry=False)
    logger.info("Feishu command queued message_id=%s task_id=%s", command.message_id, result.id)
    return str(result.id)


def process_feishu_event(payload: dict, *, bot_open_id: str, app_id: str,
                         allowed_chat_ids, pipeline, job_id: str, public_base_url: str = "") -> dict:
    command = parse_feishu_command(payload, app_id=app_id, bot_open_id=bot_open_id,
                                   allowed_chat_ids=allowed_chat_ids)
    if command is None:
        return {"status": "ignored"}
    connector = pipeline.collaboration
    identity = incident_id_for(app_id, command.chat_id, command.message_id)

    def reply(phase: str, text: str, *, persist: bool = False) -> None:
        if persist:
            record = pipeline.store.get_incident(identity)
            if phase in record.metadata.get("feishu_notifications", []):
                return
        try:
            connector.reply_text(command.message_id, text, str(uuid5(NAMESPACE_URL, identity + ":" + phase)))
        except Exception as exc:
            # Do not send provider errors, credentials or source content to the group.
            raise FeishuNotificationError(f"Feishu {phase} reply failed for {identity}") from exc
        if persist:
            record = pipeline.store.get_incident(identity)
            phases = list(record.metadata.get("feishu_notifications", []))
            pipeline.store.update_incident(identity, metadata={**record.metadata,
                                            "feishu_notifications": [*phases, phase]})

    if command.action != "start":
        reply(command.action, (command.error + "\n\n" if command.error else "") + FEISHU_COMMAND_HELP)
        return {"status": command.action}

    link = review_link(public_base_url, identity)
    with pipeline.store.execution_lock(identity) as acquired:
        if not acquired:
            raise FeishuIncidentBusy(identity)
        raw_text = json.loads(payload["event"]["message"]["content"])["text"]
        incident = pipeline.store.create_incident(IncidentRecord(
            id=identity, title=command.title, description=command.description,
            source_type="feishu_message", source_url=f"feishu://message/{command.message_id}",
            # Scope history to this group instead of querying all repositories with an empty repo ID.
            repo_id=f"feishu:{app_id}:{command.chat_id}",
            feishu_chat_ids=[command.chat_id], feishu_doc_tokens=[],
            metadata={
                "feishu_app_id": app_id, "feishu_bot_open_id": bot_open_id,
                "feishu_event_id": command.event_id, "feishu_message_id": command.message_id,
                "feishu_chat_id": command.chat_id, "feishu_sender_open_id": command.sender_open_id,
                "feishu_trigger_text": raw_text,
                "occurred_at": datetime.fromtimestamp(command.message_time, timezone.utc).isoformat(),
                "feishu_window": {"start_time": command.start_time, "end_time": command.end_time},
                "service": command.service, "environment": command.environment,
            },
        ))
        pipeline.store.create_job(job_id, identity, "collecting", "running")
        # A redelivery after process death must not silently generate duplicate evidence/versions.
        active = {IncidentStatus.COLLECTING, IncidentStatus.RETRIEVING,
                  IncidentStatus.GENERATING, IncidentStatus.VALIDATING}
        if incident.status in active:
            incident = pipeline.store.update_incident(identity, status=IncidentStatus.FAILED,
                failed_stage="collecting", error="飞书任务执行中断，请在工作台检查证据后重试。")
        was_received = incident.status == IncidentStatus.RECEIVED
        if was_received:
            window = incident.metadata["feishu_window"]
            def local_time(value):
                from .retrospective_feishu_commands import COMMAND_TIMEZONE
                return datetime.fromtimestamp(value, COMMAND_TIMEZONE).strftime("%Y-%m-%d %H:%M")
            try:
                reply("accepted", f"已受理复盘：{incident.title}\n事故 ID：{identity}\n"
                      f"取证范围：当前群，北京时间 {local_time(window['start_time'])} 至 {local_time(window['end_time'])}。\n"
                      f"正在收集证据并生成草稿，完成后仍需人工审核。\n{link}", persist=True)
            except FeishuNotificationError:
                logger.warning("Feishu acceptance reply unavailable incident_id=%s", identity)
            try:
                pipeline.run(identity)
            except Exception:
                logger.exception("Feishu retrospective failed incident_id=%s", identity)
                current = pipeline.store.get_incident(identity)
                if current.status != IncidentStatus.FAILED:
                    pipeline.store.update_incident(identity, status=IncidentStatus.FAILED,
                        failed_stage="collecting", error="飞书复盘任务失败，请查看 Worker 日志。")
            incident = pipeline.store.get_incident(identity)
        if incident.status == IncidentStatus.FAILED:
            pipeline.store.update_job(job_id, stage="failed", status="failed", error=incident.error)
            reply("failed", f"复盘生成失败。事故 ID：{identity}\n请在工作台查看原因、补充材料并重试。\n{link}", persist=True)
            return {"status": "failed", "incident_id": identity}
        pipeline.store.update_job(job_id, stage=incident.status.value, status="completed")
        if getattr(pipeline, "review_notifier", None) is not None:
            version = pipeline.store.latest_version(identity)
            if version is not None and not was_received:
                pipeline.review_notifier.on_ready(identity, version.id)
            return {"status": incident.status.value, "incident_id": identity}
        reply("completed", f"复盘草稿已生成：{incident.title}\n事故 ID：{identity}\n"
              f"请在工作台核对证据、审核并发布。\n{link}", persist=True)
        return {"status": incident.status.value, "incident_id": identity}
