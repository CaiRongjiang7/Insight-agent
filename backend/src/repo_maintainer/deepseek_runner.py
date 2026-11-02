"""DeepSeek Harness investigation adapter; writer and approval remain separate."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from pathlib import Path

from .investigation_runner import InvestigationSession, investigation_input
from .retrospective_harness_state import HARNESS_VERSION, HarnessSettings, child_environment, runtime_patch
from .retrospective_harness_trace import HarnessTrace
from .retrospective_mcp import GatewayRequest, InvestigationGateway


class DeepSeekInvestigationRunner:
    def __init__(self, settings: HarnessSettings, *, sdk_factory=None):
        self.settings = settings
        self._sdk_factory = sdk_factory

    def supports(self, generator):
        return True

    def run(self, context):
        session = InvestigationSession(context, runtime="deepseek", timeout_seconds=self.settings.timeout_seconds,
            max_calls=self.settings.max_calls, output_budget_chars=self.settings.tool_output_chars,
            tool_item_chars=self.settings.tool_item_chars, repository_search_enabled=True)
        events: queue.Queue = queue.Queue()
        trace = HarnessTrace(session)
        root = Path(self.settings.root).resolve() / session.run_id
        home, workspace = root / "home", root / "workspace"
        home.mkdir(parents=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        patch_path = root / "retrospective.patch.json"
        payload = investigation_input(context, budget_chars=self.settings.initial_context_chars,
                                      item_chars=self.settings.initial_item_chars, max_items=64)
        payload['exploration_budget'] = {
            'max_model_requests': self.settings.max_rounds, 'max_tool_calls': self.settings.max_calls,
            'tool_output_chars': self.settings.tool_output_chars, 'timeout_seconds': self.settings.timeout_seconds,
            'max_compaction_requests': self.settings.compaction_max_requests if self.settings.compaction_enabled else 0,
            'context_tokens': self.settings.context_tokens,
            'initial_context_chars': self.settings.initial_context_chars,
            'initial_item_chars': self.settings.initial_item_chars,
            'tool_item_chars': self.settings.tool_item_chars,
            'max_output_tokens': self.settings.max_output_tokens,
            'compaction_max_tokens': self.settings.compaction_max_tokens,
            'request_max_bytes': self.settings.request_max_bytes,
            'model_timeout_seconds': self.settings.model_timeout_seconds,
        }
        session.record("harness_run", {"runtime": "deepseek", "sdk_version": HARNESS_VERSION,
                       "session_id": session.run_id, "home": str(home), "workspace": str(workspace),
                       "model": self.settings.model, "api": self.settings.api, "input": payload,
                       "max_rounds": self.settings.max_rounds, "timeout_seconds": self.settings.timeout_seconds},
                       scope="harness_run")
        runtime, thread = None, None
        stop_reason, error_type = "model_error", ""
        try:
            with InvestigationGateway(session, self.settings, events) as gateway:
                patch = runtime_patch(self.settings, gateway.url, gateway.token)
                encoded = json.dumps(patch, ensure_ascii=False)
                patch_path.write_text(encoded, encoding="utf-8")
                redacted = encoded.replace(gateway.token, "<run-token>")
                session.record("harness_profile", {"patch": json.loads(redacted),
                               "sha256": hashlib.sha256(redacted.encode()).hexdigest()}, scope="runtime_profile")
                factory = self._sdk_factory
                if factory is None:
                    from deepseek_harness import DeepSeekHarness
                    factory = DeepSeekHarness
                runtime = factory(
                    provider="insight", model=self.settings.model, max_tokens=self.settings.max_output_tokens,
                    cwd=str(workspace), dsh_home=str(home), profile="sdk-minimal", patches=(str(patch_path),),
                    env=child_environment(gateway.token), initialize_timeout_seconds=min(10, self.settings.timeout_seconds),
                    request_timeout_seconds=self.settings.timeout_seconds, shutdown_timeout_seconds=1.0,
                )
                try:
                    runtime.start()
                except Exception as exc:
                    error_type = type(exc).__name__
                else:
                    def run_sdk():
                        try:
                            result = runtime.run(json.dumps(payload, ensure_ascii=False), session_id=session.run_id,
                                on_notification=lambda n: events.put(("notification", n.method, n.payload)))
                            events.put(("complete", result.finish_reason))
                        except Exception as exc:
                            events.put(("error", type(exc).__name__))

                    thread = threading.Thread(target=run_sdk, name="retrospective-harness", daemon=True)
                    thread.start()
                    while True:
                        if time.monotonic() >= gateway.deadline:
                            stop_reason = "deadline"
                            break
                        try:
                            event = events.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        if isinstance(event, GatewayRequest):
                            if event.kind == "tool":
                                session.bridge_ids.add(event.payload["bridge_call_id"])
                            gateway.dispatch(event)  # persistence failures deliberately propagate
                        elif event[0] == "notification":
                            trace.notification(event[1], event[2])
                        elif event[0] == "complete":
                            stop_reason = gateway.stop_reason or ("no_tool_calls" if event[1] == "completed" and gateway.model_requests else "model_error")
                            if stop_reason == "model_error":
                                error_type = "HarnessTurnError"
                            break
                        elif event[0] == "error":
                            stop_reason, error_type = gateway.stop_reason or "model_error", event[1]
                            break
                # Shut down while the gateway still exists, then reject any pending work.
                if runtime is not None:
                    runtime.close()
                    runtime = None
                if thread:
                    thread.join(timeout=2)
                model_requests = gateway.model_requests
                compaction_requests, compaction_errors = gateway.compaction_requests, gateway.compaction_errors
                compaction_usage = gateway.compaction_usage
        finally:
            if runtime is not None:
                runtime.close()
            if thread:
                thread.join(timeout=2)
            # Never retain even the expired loopback bearer in the saved profile.
            if patch_path.exists():
                patch_path.write_text(redacted if "redacted" in locals() else "[]", encoding="utf-8")
        return session.finish(stop_reason, model_requests, error_type,
                              session_id=session.run_id, runtime_events=trace.event_count, model_usage=trace.usage + compaction_usage,
                              compaction_enabled=self.settings.compaction_enabled,
                              compaction_requests=compaction_requests,
                              compaction_proxy_errors=compaction_errors,
                              compaction_native_errors=len(trace.compaction_failures),
                              compaction_errors=max(compaction_errors, len(trace.compaction_failures)),
                              compaction_failures=trace.compaction_failures,
                              compaction_events=trace.compaction_events,
                              total_model_requests=model_requests + compaction_requests)
