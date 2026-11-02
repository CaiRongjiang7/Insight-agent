"""Configuration, authorization, dispatch limits and failure isolation."""

import json
import queue
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repo_maintainer.deepseek_runner import DeepSeekInvestigationRunner
from repo_maintainer.investigation_runner import InvestigationContext, InvestigationSession, StandardInvestigationRunner
from repo_maintainer.retrospective_harness_state import HarnessSettings, child_environment, runtime_patch
from repo_maintainer.retrospective_harness_trace import HarnessTrace
from repo_maintainer.retrospective_mcp import GatewayRequest, InvestigationGateway
from repo_maintainer.retrospective_models import IncidentCreate, IncidentRecord
from repo_maintainer.retrospective_runtime import create_investigation_runner_from_env
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore


def context():
    store = InMemoryRetrospectiveStore()
    incident = store.create_incident(IncidentRecord(
        **IncidentCreate(title="recovery", repo_id="repo").model_dump()))
    return InvestigationContext(incident, [], [], [], store, object(), lambda *_: [])


def settings(tmp_path, **changes):
    return HarnessSettings(model="model", api_key="secret", base_url="http://127.0.0.1:1/v1", root=tmp_path, **changes)


@pytest.mark.parametrize("values", [dict(timeout_seconds=0), dict(timeout_seconds=float("nan")),
    dict(max_rounds=0), dict(max_rounds=257), dict(max_output_tokens=100), dict(api="unknown"),
    dict(max_calls=0), dict(max_calls=513), dict(tool_output_chars=999), dict(tool_output_chars=4_000_001),
    dict(model_timeout_seconds=float('nan')), dict(model_timeout_seconds=601),
    dict(initial_context_chars=999), dict(initial_item_chars=499), dict(tool_item_chars=120001),
    dict(request_max_bytes=64000001)])
def test_invalid_budgets_and_protocol_are_rejected(tmp_path, values):
    with pytest.raises(ValueError):
        settings(tmp_path, **values)


@pytest.mark.parametrize("url", ["file:///tmp/model", "https://user:password@model.test/v1", "https://model.test/v1?key=secret"])
def test_credentials_cannot_hide_in_url(tmp_path, url):
    with pytest.raises(ValueError):
        replace(settings(tmp_path), base_url=url)


def test_default_and_disabled_modes_need_no_sdk():
    with patch.dict("os.environ", {}, clear=True):
        assert isinstance(create_investigation_runner_from_env(), StandardInvestigationRunner)
    with patch.dict("os.environ", {"REVIEW_INVESTIGATION_RUNNER": "deepseek"}, clear=True):
        assert isinstance(create_investigation_runner_from_env(enabled=False), StandardInvestigationRunner)
    with patch.dict("os.environ", {"REVIEW_INVESTIGATION_RUNNER": "wrong"}, clear=True):
        with pytest.raises(ValueError):
            create_investigation_runner_from_env()


def test_enabled_runtime_wiring_keeps_writer_settings_and_validates_versions():
    env = {"REVIEW_INVESTIGATION_RUNNER": "deepseek", "OPENAI_MODEL": "writer-model", "OPENAI_API_KEY": "key",
           "REVIEW_HARNESS_MODEL": "investigator-model", "OPENAI_ENABLE_THINKING": "0"}
    with patch.dict("os.environ", env, clear=True), patch(
        "repo_maintainer.retrospective_harness_state.validate_installation"
    ) as validate:
        runner = create_investigation_runner_from_env()
    validate.assert_called_once()
    assert runner.settings.model == "investigator-model"
    assert runner.settings.base_url == "https://api.openai.com/v1"
    assert runner.settings.enable_thinking is False
    assert (runner.settings.max_rounds, runner.settings.max_calls, runner.settings.tool_output_chars,
            runner.settings.timeout_seconds) == (40, 80, 600000, 600)
    assert "key" not in repr(runner.settings)


def test_child_environment_does_not_inherit_product_credentials():
    with patch.dict("os.environ", {"GITHUB_TOKEN": "github", "DATABASE_URL": "db", "OPENAI_API_KEY": "key",
                                  "HTTPS_PROXY": "proxy", "PATH": "bin"}, clear=True):
        env = child_environment("loopback")
    assert env["PATH"] == "bin"
    assert all(env[name] == "" for name in ("GITHUB_TOKEN", "DATABASE_URL", "OPENAI_API_KEY", "HTTPS_PROXY"))
    assert env["INSIGHT_HARNESS_TOKEN"] == "loopback"


def test_profile_has_only_domain_tools_and_no_upstream_secret(tmp_path):
    patch_rows = runtime_patch(settings(tmp_path), "http://127.0.0.1:1234", "local-token")
    disabled = {r["id"] for r in patch_rows if r.get("disabled")}
    assert {"persistent-bash", "persistent-pwsh", "tool-fs", "tool-working-directory", "llm-retry",
            "session-log-deepseek"} <= disabled
    assert 'compaction-basic' not in disabled
    compact = next(r for r in patch_rows if r['id'] == 'compaction-basic')
    assert compact['config']['summarizationProvider'] == 'insight-compaction'
    inserted = patch_rows[-1]["insert"]
    assert len(inserted) == 2
    assert "secret" not in json.dumps(patch_rows)


def test_gateway_executes_tools_on_the_pipeline_thread():
    session = InvestigationSession(context())
    seen = []
    original = session.executor.execute
    session.executor.execute = lambda *args: (seen.append(threading.get_ident()), original(*args))[1]
    gateway = InvestigationGateway(session, SimpleNamespace(timeout_seconds=30), queue.Queue())
    request = GatewayRequest("tool", {"name": "unknown", "arguments": {}, "bridge_call_id": "bridge-test"})
    gateway.dispatch(request)
    assert seen == [threading.get_ident()]
    assert request.future.result()["error"]["code"] == "UNKNOWN_TOOL"
    assert request.future.result()["bridge_call_id"] == "bridge-test"


def test_gateway_persistence_failure_is_not_relabelled_as_model_failure(tmp_path):
    session = InvestigationSession(context())
    gateway = InvestigationGateway(session, settings(tmp_path), queue.Queue())
    session.record = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("database unavailable"))
    request = GatewayRequest("model_request", {"round": 1})
    with pytest.raises(OSError):
        gateway.dispatch(request)
    with pytest.raises(RuntimeError):
        request.future.result()


@pytest.mark.parametrize("identity", [{"toolCallId": "native-call"}, {}, {"callId": "wrong-field"}])
def test_trace_mapping_requires_canonical_nonempty_tool_call_id(identity):
    c = context()
    session = InvestigationSession(c)
    session.bridge_ids.add("bridge-test")
    trace = HarnessTrace(session)
    trace.notification("session.event", {"event": {"type": "tool/result", "data": {"message": {
        **identity, "content": [{"type": "text", "text": json.dumps({"bridge_call_id": "bridge-test"})}],
    }}}})
    mappings = [r.payload["request"] for r in c.store.list_generation_contexts(c.incident.id)
                if r.payload["purpose"] == "harness_call_mapping"]
    assert mappings == ([{"bridge_call_id": "bridge-test", "harness_call_id": "native-call"}]
                        if identity.get("toolCallId") else [])


def test_sdk_start_failure_returns_a_recorded_fallback(tmp_path):
    pytest.importorskip("mcp")
    instances = []

    class BrokenSDK:
        def __init__(self, **kwargs):
            self.closed = False
            instances.append(self)
        def start(self):
            raise RuntimeError("startup failed")
        def close(self):
            self.closed = True

    c = context()
    *_, summary = DeepSeekInvestigationRunner(settings(tmp_path), sdk_factory=BrokenSDK).run(c)
    assert summary["stop_reason"] == "model_error" and summary["rounds"] == 0
    assert instances[0].closed
    assert any(r.payload["purpose"] == "tool_exploration_summary" for r in c.store.list_generation_contexts(c.incident.id))
    assert "local-token" not in next(tmp_path.glob("*/retrospective.patch.json")).read_text()


def test_deadline_closes_sdk_and_joins_the_run_thread(tmp_path):
    pytest.importorskip("mcp")
    stopped = threading.Event()

    class WaitingSDK:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def run(self, *args, **kwargs):
            stopped.wait(10)
            return SimpleNamespace(finish_reason="aborted")
        def close(self):
            stopped.set()

    *_, summary = DeepSeekInvestigationRunner(settings(tmp_path, timeout_seconds=1), sdk_factory=WaitingSDK).run(context())
    assert summary["stop_reason"] == "deadline"
    assert stopped.is_set()
    assert not any(t.name == "retrospective-harness" and t.is_alive() for t in threading.enumerate())


@pytest.mark.parametrize("case,expected", [("unauthorized", 401), ("foreign_tool", 400),
    ("wrong_model", 400), ("oversized", 413), ("output_limit", 400), ("round_limit", 429)])
def test_model_gateway_rejects_out_of_scope_or_over_budget_requests(tmp_path, case, expected):
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    gateway = InvestigationGateway(InvestigationSession(context()), settings(tmp_path, request_max_bytes=256_000), queue.Queue())
    body = {"model": "model", "tools": [{"type": "function", "function": {"name": "mcp__retrospective__" + name}}
            for name in ("get_related_code", "search_feishu_context", "search_historical_cases")], "max_tokens": 700}
    if case == "foreign_tool":
        body["tools"].append({"function": {"name": "bash"}})
    if case == "wrong_model":
        body["model"] = "other-model"
    if case == "output_limit":
        body["max_tokens"] = gateway.config.max_output_tokens + 1
    if case == "round_limit":
        gateway.model_requests = gateway.config.max_rounds
    headers = {"Authorization": "Bearer " + ("wrong" if case == "unauthorized" else gateway.token)}
    with TestClient(gateway._app(), base_url="http://127.0.0.1") as client:
        response = client.post("/model/chat/completions", headers=headers,
                               content="x" * 256001 if case == "oversized" else json.dumps(body))
    assert response.status_code == expected
    assert gateway.events.empty()
