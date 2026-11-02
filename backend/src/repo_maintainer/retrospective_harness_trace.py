"""Preserve runtime events without promoting them to incident evidence."""

from __future__ import annotations

import json


class HarnessTrace:
    def __init__(self, session):
        self.session = session
        self.event_count = 0
        self.usage: list[dict] = []
        self.compaction_events: list[str] = []
        self.compaction_failures: list[dict] = []

    def notification(self, method, payload):
        if method not in {"session.event", "session.status"}:
            return
        self.event_count += 1
        self.session.record("harness_event", {"method": method, "payload": payload}, scope="harness_event")
        event = payload.get("event", {})
        data = event.get("data", {})
        if event.get("type") == "assistant/message" and isinstance(data.get("usage"), dict):
            self.usage.append(data["usage"])
        if event.get('type', '').startswith('compaction/'):
            self.compaction_events.append(event['type'])
            if event['type'] == 'compaction/end' and not data.get('error'):
                self.session.executor.on_compaction()
            if event['type'] == 'compaction/end' and data.get('error'):
                self.compaction_failures.append({'compaction_id': data.get('compactionId'),
                                                 'error': str(data['error'])[:1000]})
        if event.get("type") == "tool/result":
            message = data.get("message", {})
            call_id = message.get("toolCallId")
            for block in message.get("content", []):
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                try:
                    body = json.loads(block["text"])
                except (ValueError, KeyError, TypeError):
                    continue
                bridge_id = body.get("bridge_call_id") if isinstance(body, dict) else None
                if (bridge_id and bridge_id in self.session.bridge_ids
                        and isinstance(call_id, str) and call_id):
                    self.session.record("harness_call_mapping", {"bridge_call_id": bridge_id,
                                        "harness_call_id": call_id}, scope="call_mapping")
