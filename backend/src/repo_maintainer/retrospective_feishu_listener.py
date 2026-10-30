"""Optional standalone official Feishu WebSocket listener; no inbound public endpoint."""

from __future__ import annotations

import json
import logging
import os

from .retrospective_connectors import FeishuConnector, RedisTokenCache
from .retrospective_feishu_bot import enqueue_feishu_event


def main() -> None:
    import lark_oapi as lark
    from celery import Celery

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_id = os.environ["FEISHU_APP_ID"].strip()
    app_secret = os.environ["FEISHU_APP_SECRET"].strip()
    redis_url = os.environ["REDIS_URL"].strip()
    chat_ids = {value.strip() for value in os.getenv("FEISHU_CHAT_IDS", "").split(",") if value.strip()}
    if not app_id or not app_secret or not chat_ids:
        raise ValueError("Feishu listener requires app credentials and a nonempty FEISHU_CHAT_IDS allowlist")
    base_url = os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/")
    connector = FeishuConnector(app_id=app_id, app_secret=app_secret,
        cache=RedisTokenCache(redis_url), base_url=base_url)
    bot_open_id = connector.bot_open_id()
    queue = Celery("feishu-event-listener", broker=redis_url)
    # Bound the broker wait within the event ACK deadline; disable long publish retries.
    queue.conf.update(broker_connection_timeout=1, task_publish_retry=False,
        broker_transport_options={"socket_connect_timeout": 1, "socket_timeout": 1,
                                  "retry_on_timeout": False})

    def on_message(data) -> None:
        enqueue_feishu_event(json.loads(lark.JSON.marshal(data)), queue=queue,
            app_id=app_id, bot_open_id=bot_open_id, allowed_chat_ids=chat_ids)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    logging.getLogger(__name__).info("Starting Feishu listener for %s configured group(s)", len(chat_ids))
    # SDK INFO includes the signed WebSocket URL; WARNING avoids logging that credential.
    lark.ws.Client(app_id, app_secret, event_handler=handler, domain=base_url,
                   log_level=lark.LogLevel.WARNING).start()


if __name__ == "__main__":
    main()
