"""Version-scoped review reminders; notification failure never invalidates a draft."""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5

from .retrospective_models import IncidentStatus

logger = logging.getLogger(__name__)


def review_link(base_url: str, incident_id: str) -> str:
    if not base_url.strip():
        return "请在复盘工作台按事故 ID 查看。"
    base_url = base_url.strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("REVIEW_PUBLIC_BASE_URL must be an HTTP(S) URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("REVIEW_PUBLIC_BASE_URL must not contain query or fragment")
    return base_url.rstrip("/") + "/retrospective?" + urlencode({"incident_id": incident_id})


class ReviewNotifier:
    def __init__(self, store, connector, public_base_url: str, enqueue_retry=None):
        self.store = store
        self.connector = connector
        self.public_base_url = public_base_url
        self.enqueue_retry = enqueue_retry
        review_link(public_base_url, "validation")

    def notify(self, incident_id: str, version_id: str) -> dict:
        """Called while holding the shared incident execution lock."""
        incident = self.store.get_incident(incident_id)
        version = self.store.latest_version(incident_id)
        if incident.status != IncidentStatus.PENDING_REVIEW or version is None or version.id != version_id:
            return {"status": "skipped"}
        # A chat-triggered incident stays in its originating group.
        origin = incident.metadata.get("feishu_chat_id")
        targets = [origin] if origin else list(dict.fromkeys(incident.feishu_chat_ids))
        if not targets:
            self._error(incident_id, "未配置审核通知群，请设置问题的飞书群或 FEISHU_CHAT_IDS。")
            return {"status": "unconfigured"}
        if not self.public_base_url.strip():
            self._error(incident_id, "请配置 REVIEW_PUBLIC_BASE_URL，使审核通知包含可访问的页面链接。")
            raise RuntimeError("REVIEW_PUBLIC_BASE_URL is required for review notifications")
        text = (f"复盘待人工审核：{version.document.title}\n事故 ID：{incident_id} · v{version.version}\n"
                "请核对结论与引用；可直接保存人工修改，或填写审核意见交给 Agent 重新取证修订。\n"
                "审核通过并发布后才会写入历史知识库。\n"
                + review_link(self.public_base_url, incident_id))
        for chat_id in targets:
            incident = self.store.get_incident(incident_id)
            delivered = dict(incident.metadata.get("review_notifications", {}))
            key = f"{version_id}:{chat_id}"
            if key in delivered:
                continue
            try:
                self.connector.send_text(chat_id, text, str(uuid5(NAMESPACE_URL, f"review:{incident_id}:{key}")))
            except Exception as exc:
                self._error(incident_id, "飞书待审核通知发送失败，可重试通知；复盘草稿已保留。")
                raise RuntimeError(f"Review notification failed for {incident_id}") from exc
            delivered[key] = True
            current = self.store.get_incident(incident_id)
            self.store.update_incident(incident_id, metadata={**current.metadata, "review_notifications": delivered})
        self._error(incident_id, "")
        return {"status": "delivered"}

    def _error(self, incident_id, message):
        current = self.store.get_incident(incident_id)
        self.store.update_incident(incident_id, metadata={**current.metadata, "review_notification_error": message})

    def on_ready(self, incident_id: str, version_id: str) -> None:
        try:
            self.notify(incident_id, version_id)
        except Exception:
            logger.exception("Review reminder failed incident_id=%s", incident_id)
            if self.enqueue_retry is not None:
                try:
                    self.enqueue_retry(incident_id, version_id)
                except Exception:
                    logger.exception("Review reminder retry enqueue failed incident_id=%s", incident_id)
