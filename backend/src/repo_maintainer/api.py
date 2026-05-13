from __future__ import annotations

import asyncio
import json
from pathlib import Path
from queue import Empty
from time import monotonic
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import PlatformConfig
from .flow_debug import flow_log, subscribe_flow_events, unsubscribe_flow_events
from .frontend_service import FrontendDataService
from .platform import RepoAutonomyPlatform
from .pr_listener import PRListener
from .review_workflow import ReviewWorkflowEngine
from .wiki_publisher import WikiPublisherService


class RepoUpsertRequest(BaseModel):
    id: str | None = None
    name: str
    url: str = ""
    owner: str = "Unknown"
    status: str = "normal"
    ci_status: str = "normal"
    health_score: int = 88
    agent_name: str = "maintainer-orchestrator"
    description: str = ""
    local_path: str = ""


class ManualFixRequest(BaseModel):
    instructions: str = Field(..., min_length=1)
    max_attempts: int = 1
    remote_delivery_confirmed: bool = False
    async_mode: bool = False


class RetryRequest(BaseModel):
    max_attempts: int = 1
    remote_delivery_confirmed: bool = False
    async_mode: bool = False


class RemoteDeliveryRequest(BaseModel):
    async_mode: bool = False


class CiSyncRequest(BaseModel):
    provider: str
    owner: str | None = None
    repo: str | None = None
    branch: str | None = None
    project_id: str | None = None
    job_name: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class CiRerunRequest(BaseModel):
    provider: str
    run_id: str
    owner: str | None = None
    repo: str | None = None
    project_id: str | None = None
    job_name: str | None = None
    failed_only: bool = False


class TaskPriorityRequest(BaseModel):
    task_ids: list[str]
    priority: str


class IncidentDeleteRequest(BaseModel):
    incident_ids: list[str]


# ===== Refactored Models (Phase 6) =====

class BugReportCreateRequest(BaseModel):
    repo_id: str = ""
    repo_name: str = ""
    title: str = ""
    description: str = ""
    source_type: str = "manual"          # pr | log | chat | manual
    pr_id: str | None = None
    commit_sha: str | None = None
    repo_url: str | None = None
    logs_excerpt: str | None = None
    full_logs: str | None = None
    bug_category: str = "unknown"

class BugReportUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    bug_category: str | None = None
    review_document: dict[str, Any] | None = None
    extraction_confidence: float | None = None
    semantic_summary: str | None = None

class ExtractionTriggerRequest(BaseModel):
    bug_report_id: str
    async_mode: bool = False

class ReviewActionRequest(BaseModel):
    notes: str = ""

class KnowledgeUpsertRequest(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    error_types: list[str] = Field(default_factory=list)
    action_template: str = ""
    keywords: list[str] = Field(default_factory=list)
    applicable_context: str = ""
    active: bool = True

class KnowledgeRetireRequest(BaseModel):
    reason: str = "manual retire"

class WebhookManualRequest(BaseModel):
    repo_id: str
    pr_url: str | None = None
    pr_title: str | None = None
    description: str | None = None
    logs: str | None = None


class SkillUpsertRequest(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    error_types: list[str] = Field(default_factory=list)
    action_template: str = ""
    active: bool = True


class RetireSkillRequest(BaseModel):
    reason: str = "manual retire"


class ThresholdUpdateRequest(BaseModel):
    thresholds: dict[str, dict[str, float]]


class LearningRetryRequest(BaseModel):
    actor: str = "api-user"
    reason: str = Field(..., min_length=1)
    priority: int | None = Field(default=None, ge=0, le=100)


class LearningDismissRequest(BaseModel):
    actor: str = "api-user"
    reason: str = Field(..., min_length=1)


class BugReportDeleteRequest(BaseModel):
    ids: list[str]


def create_app(config: PlatformConfig | None = None) -> FastAPI:
    root_dir = Path(__file__).resolve().parents[2]
    platform = RepoAutonomyPlatform(config or PlatformConfig.default(root_dir))
    service = FrontendDataService(platform)

    # Phase 6: New infrastructure services
    review_workflow = ReviewWorkflowEngine(storage_dir=root_dir / ".review_logs")
    wiki_publisher = WikiPublisherService(output_dir=root_dir / ".wiki_output", mode="local")
    pr_listener = PRListener()  # webhook listener (auto-detect GitHub/GitLab)

    app = FastAPI(
        title="Bug Review Knowledge Pipeline API",
        version="1.0.0",
        description="API backend for the self-evolving bug-review knowledge pipeline.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=platform.config.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.platform = platform
    app.state.service = service
    # Phase 6: store new services on app state
    app.state.review_workflow = review_workflow
    app.state.wiki_publisher = wiki_publisher
    app.state.pr_listener = pr_listener

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "runtime_backend": platform.config.runtime.backend}

    @app.get("/api/dashboard/overview")
    def dashboard_overview(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        return service.dashboard_overview(days=days)

    @app.get("/api/system/status")
    def system_status() -> dict[str, Any]:
        return service.system_status()

    @app.get("/api/notifications")
    def notifications() -> list[dict[str, Any]]:
        return service.notifications()

    @app.get("/api/search")
    def search(q: str = Query(default="", alias="q"), limit: int = Query(default=8, ge=1, le=20)) -> list[dict[str, Any]]:
        return service.search(q, limit=limit)

    @app.get("/api/providers")
    def providers() -> dict[str, Any]:
        return service.provider_options()

    @app.get("/api/ci/providers")
    def ci_providers() -> dict[str, Any]:
        return service.ci_provider_options()

    @app.get("/api/ci/jenkins/jobs")
    def jenkins_jobs() -> list[dict[str, Any]]:
        try:
            return service.list_jenkins_jobs()
        except (ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/repos")
    def repos(name: str | None = None, status: str | None = None, agent: str | None = None, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        return service.list_repositories(name=name, status=status, agent=agent, limit=limit)

    @app.post("/api/repos")
    def create_repo(payload: RepoUpsertRequest) -> dict[str, Any]:
        return service.create_repository(payload.model_dump())

    @app.get("/api/repos/{repo_id}")
    def repo_detail(repo_id: str) -> dict[str, Any]:
        try:
            return service.get_repository_detail(repo_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/repos/{repo_id}")
    def update_repo(repo_id: str, payload: RepoUpsertRequest) -> dict[str, Any]:
        try:
            return service.update_repository(repo_id, payload.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/repos/{repo_id}")
    def delete_repo(repo_id: str) -> dict[str, Any]:
        service.delete_repository(repo_id)
        return {"success": True}

    @app.get("/api/incidents")
    def incidents(
        limit: int = Query(default=50, ge=1, le=200),
        incident_type: str | None = None,
        status: str | None = None,
        repo_id: str | None = None,
        provider: str | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        return service.list_incidents(limit=limit, incident_type=incident_type, status=status, repo_id=repo_id, provider=provider, keyword=keyword)

    @app.post("/api/ci/sync")
    def sync_ci(payload: CiSyncRequest) -> dict[str, Any]:
        flow_log("api.ci_sync.request", payload=payload.model_dump())
        try:
            result = service.sync_ci_provider(
                payload.provider,
                owner=payload.owner,
                repo=payload.repo,
                branch=payload.branch,
                project_id=payload.project_id,
                job_name=payload.job_name,
                limit=payload.limit,
            )
            flow_log("api.ci_sync.response", provider=result.get("provider"), incidents=len(result.get("incidents", [])))
            return result
        except (ValueError, RuntimeError, PermissionError) as exc:
            flow_log("api.ci_sync.error", error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ci/rerun")
    def rerun_ci(payload: CiRerunRequest) -> dict[str, Any]:
        try:
            return service.rerun_ci_run(
                payload.provider,
                run_id=payload.run_id,
                owner=payload.owner,
                repo=payload.repo,
                project_id=payload.project_id,
                job_name=payload.job_name,
                failed_only=payload.failed_only,
            )
        except (ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(incident_id: str) -> dict[str, Any]:
        try:
            return service.get_incident_detail(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/incidents/delete")
    def delete_incidents(payload: IncidentDeleteRequest) -> dict[str, Any]:
        return service.delete_incidents(payload.incident_ids)

    @app.get("/api/reports/{trace_id}")
    def report(trace_id: str) -> dict[str, str]:
        try:
            return service.get_report(trace_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/retry")
    def retry_incident(incident_id: str, payload: RetryRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        flow_log("api.retry.request", incident_id=incident_id, payload=payload.model_dump())
        try:
            if payload.async_mode:
                result = service.enqueue_retry_incident(
                    incident_id,
                    max_attempts=payload.max_attempts,
                    remote_delivery_confirmed=payload.remote_delivery_confirmed,
                )
                background_tasks.add_task(service.run_retry_incident_job, str(result["job_id"]))
                flow_log("api.retry.accepted", incident_id=incident_id, result=result)
                return result
            result = service.retry_incident(
                incident_id,
                max_attempts=payload.max_attempts,
                remote_delivery_confirmed=payload.remote_delivery_confirmed,
            )
            flow_log("api.retry.response", incident_id=incident_id, result=result)
            return result
        except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
            flow_log("api.retry.error", incident_id=incident_id, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/manual-fix")
    def manual_fix(incident_id: str, payload: ManualFixRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        flow_log("api.manual_fix.request", incident_id=incident_id, payload=payload.model_dump())
        try:
            if payload.async_mode:
                result = service.enqueue_manual_fix_incident(
                    incident_id,
                    payload.instructions,
                    max_attempts=payload.max_attempts,
                    remote_delivery_confirmed=payload.remote_delivery_confirmed,
                )
                background_tasks.add_task(service.run_manual_fix_incident_job, str(result["job_id"]))
                flow_log("api.manual_fix.accepted", incident_id=incident_id, result=result)
                return result
            result = service.manual_fix_incident(
                incident_id,
                payload.instructions,
                max_attempts=payload.max_attempts,
                remote_delivery_confirmed=payload.remote_delivery_confirmed,
            )
            flow_log("api.manual_fix.response", incident_id=incident_id, result=result)
            return result
        except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
            flow_log("api.manual_fix.error", incident_id=incident_id, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/repairs/{trace_id}/remote-delivery")
    def deliver_repair(trace_id: str, background_tasks: BackgroundTasks, payload: RemoteDeliveryRequest | None = None) -> dict[str, Any]:
        payload = payload or RemoteDeliveryRequest()
        flow_log("api.remote_delivery.request", trace_id=trace_id)
        try:
            if payload.async_mode:
                result = service.enqueue_deliver_repair_trace(trace_id)
                background_tasks.add_task(service.run_deliver_repair_trace_job, str(result["job_id"]))
                flow_log("api.remote_delivery.accepted", trace_id=trace_id, result=result)
                return result
            result = service.deliver_repair_trace(trace_id)
            flow_log("api.remote_delivery.response", trace_id=trace_id, result=result)
            return result
        except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
            flow_log("api.remote_delivery.error", trace_id=trace_id, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/repair-jobs/{job_id}")
    def repair_job(job_id: str) -> dict[str, Any]:
        try:
            return service.repair_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/incidents/{incident_id}/agent-workflow")
    def agent_workflow(incident_id: str) -> dict[str, Any]:
        return service.agent_workflow(incident_id)

    @app.get("/api/incidents/{incident_id}/agent-workflow/stream")
    async def agent_workflow_stream(
        incident_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        once: bool = Query(default=False),
    ) -> StreamingResponse:
        def parse_last_event_id() -> int:
            raw = request.headers.get("last-event-id", "")
            try:
                return max(int(raw), after)
            except ValueError:
                return after

        def encode_sse(event: str, payload: dict[str, Any], event_id: int | None = None) -> str:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            prefix = f"id: {event_id}\n" if event_id is not None else ""
            return f"{prefix}event: {event}\ndata: {body}\n\n"

        async def event_stream():
            cursor = parse_last_event_id()
            signal_queue = subscribe_flow_events(lambda record: str(record.get("incident_id") or "") == incident_id)
            last_keepalive = monotonic()
            flow_log("api.agent_workflow.stream.start", workflow_incident_id=incident_id, after=cursor)
            try:
                while True:
                    if await request.is_disconnected():
                        flow_log("api.agent_workflow.stream.disconnected", workflow_incident_id=incident_id, after=cursor)
                        break
                    delta = service.agent_workflow_delta(incident_id, after_event_index=cursor)
                    event_count = int(delta.get("event_count") or cursor)
                    has_delta = event_count > cursor or bool(delta.get("nodes")) or bool(delta.get("links")) or bool(delta.get("node_updates"))
                    if has_delta:
                        cursor = event_count
                        flow_log(
                            "api.agent_workflow.stream.delta",
                            workflow_incident_id=incident_id,
                            event_count=event_count,
                            nodes=len(delta.get("nodes", [])),
                            links=len(delta.get("links", [])),
                            node_updates=len(delta.get("node_updates", [])),
                        )
                        yield encode_sse("workflow_delta", delta, cursor)
                        last_keepalive = monotonic()
                        if once:
                            break
                    timeout = max(0.1, 15 - (monotonic() - last_keepalive))
                    try:
                        await asyncio.to_thread(signal_queue.get, True, timeout)
                    except Empty:
                        yield ": keepalive\n\n"
                        last_keepalive = monotonic()
            finally:
                unsubscribe_flow_events(signal_queue)
                flow_log("api.agent_workflow.stream.end", workflow_incident_id=incident_id, after=cursor)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/incidents/{incident_id}/terminate")
    def terminate_incident(incident_id: str) -> dict[str, Any]:
        return service.terminate_incident(incident_id)

    @app.get("/api/tasks")
    def tasks(limit: int = Query(default=100, ge=1, le=300)) -> list[dict[str, Any]]:
        return service.list_tasks(limit=limit)

    @app.post("/api/tasks/priority")
    def update_priority(payload: TaskPriorityRequest) -> list[dict[str, Any]]:
        return service.update_task_priority(payload.task_ids, payload.priority)

    @app.post("/api/tasks/{task_id}/terminate")
    def terminate_task(task_id: str) -> dict[str, Any]:
        try:
            return service.terminate_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/skills")
    def skills(limit: int = Query(default=100, ge=1, le=300)) -> list[dict[str, Any]]:
        return service.list_skills(limit=limit)

    @app.post("/api/skills")
    def create_skill(payload: SkillUpsertRequest) -> dict[str, Any]:
        return service.create_skill(payload.model_dump())

    @app.get("/api/skills/{skill_id}")
    def skill_detail(skill_id: str) -> dict[str, Any]:
        try:
            return service.get_skill_detail(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/skills/{skill_id}")
    def update_skill(skill_id: str, payload: SkillUpsertRequest) -> dict[str, Any]:
        try:
            return service.update_skill(skill_id, payload.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/{skill_id}/retire")
    def retire_skill(skill_id: str, payload: RetireSkillRequest) -> dict[str, Any]:
        try:
            return service.retire_skill(skill_id, payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/{skill_id}/restore")
    def restore_skill(skill_id: str) -> dict[str, Any]:
        try:
            return service.restore_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/contexts")
    def contexts(repo_id: str | None = None) -> dict[str, Any]:
        return service.context_overview(repo_id=repo_id)

    @app.delete("/api/contexts/expired")
    def clear_expired_contexts() -> dict[str, Any]:
        return service.clear_expired_contexts()

    @app.get("/api/observability")
    def observability(days: int = Query(default=30, ge=1, le=180)) -> dict[str, Any]:
        return service.observability_snapshot(days=days)

    @app.put("/api/observability/thresholds")
    def update_thresholds(payload: ThresholdUpdateRequest) -> dict[str, dict[str, float]]:
        return service.update_thresholds(payload.thresholds)

    @app.get("/api/learning/status")
    def learning_status() -> dict[str, Any]:
        return platform.learning_status()

    @app.get("/api/learning/jobs")
    def learning_jobs(status: str | None = None, limit: int = Query(default=100, ge=1, le=300)) -> list[dict[str, Any]]:
        return platform.learning_jobs(status=status, limit=limit)

    @app.post("/api/learning/jobs/{trace_id}/retry")
    def retry_learning_job(trace_id: str, payload: LearningRetryRequest) -> dict[str, Any]:
        try:
            return platform.retry_learning_job(trace_id, actor=payload.actor, reason=payload.reason, priority=payload.priority)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/learning/jobs/{trace_id}/dismiss")
    def dismiss_learning_job(trace_id: str, payload: LearningDismissRequest) -> dict[str, Any]:
        try:
            return platform.dismiss_learning_job(trace_id, actor=payload.actor, reason=payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ===== Phase 6: New API Endpoints ===========================================

    # --- Bug Reports (new core endpoint) ---

    @app.get("/api/bug-reports")
    def bug_reports(
        limit: int = Query(default=50, ge=1, le=200),
        bug_category: str | None = None,
        review_status: str | None = None,
        source_type: str | None = None,
        repo_id: str | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        reports = service.list_incidents(
            limit=limit,
            incident_type=bug_category,
            status=review_status,
            repo_id=repo_id,
            keyword=keyword,
        )
        for r in reports:
            r.setdefault("source_type", "manual")
            if not r.get("review_status"):
                r["review_status"] = "draft"
            r.setdefault("bug_category", r.get("error_type", "unknown"))
        return reports

    @app.post("/api/bug-reports")
    def create_bug_report(payload: BugReportCreateRequest) -> dict[str, Any]:
        data = payload.model_dump()
        maybe_repo_id = data.pop("repo_id", None)
        return service.create_incident(maybe_repo_id, data)

    @app.get("/api/bug-reports/{bug_report_id}")
    def bug_report_detail(bug_report_id: str) -> dict[str, Any]:
        try:
            return service.get_incident_detail(bug_report_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/bug-reports/{bug_report_id}")
    def update_bug_report(bug_report_id: str, payload: BugReportUpdateRequest) -> dict[str, Any]:
        try:
            return service.update_incident_fields(bug_report_id, payload.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/bug-reports/delete")
    def delete_bug_reports(payload: BugReportDeleteRequest) -> dict[str, Any]:
        return service.delete_incidents(payload.ids)

    # --- Extractions (LLM trigger) ---

    @app.post("/api/extractions")
    def trigger_extraction(payload: ExtractionTriggerRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            if payload.async_mode:
                result = service.enqueue_retry_incident(payload.bug_report_id, max_attempts=1)
                background_tasks.add_task(service.run_retry_incident_job, str(result["job_id"]))
                return result
            return service.retry_incident(payload.bug_report_id, max_attempts=1)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/extractions/{job_id}")
    def extraction_detail(job_id: str) -> dict[str, Any]:
        try:
            return service.repair_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # --- Bug report SSE stream ---

    @app.get("/api/bug-reports/{bug_report_id}/stream")
    async def bug_report_stream(
        bug_report_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        # Reuse existing agent workflow stream for compatibility
        return await agent_workflow_stream(bug_report_id, request, after=after)

    # --- Knowledge Base (new endpoints) ---

    @app.get("/api/knowledge")
    def knowledge_entries(
        limit: int = Query(default=100, ge=1, le=300),
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        entries = service.list_skills(limit=limit)
        for e in entries:
            e.setdefault("keywords", e.get("triggers", e.get("keywords", [])))
            e.setdefault("action_template", e.get("action_template", ""))
            e.setdefault("applicable_context", e.get("applicable_context", ""))
            e.setdefault("related_bug_ids", e.get("related_bug_ids", []))
        return entries

    @app.post("/api/knowledge")
    def create_knowledge(payload: KnowledgeUpsertRequest) -> dict[str, Any]:
        return service.create_skill(payload.model_dump())

    @app.get("/api/knowledge/search")
    def search_knowledge(
        q: str = Query(default="", alias="q"),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> list[dict[str, Any]]:
        """Keyword-based search within the knowledge base only (BM25-style)."""
        if not q or not q.strip():
            return []
        matched = platform.skill_repository.search_by_keyword(q.strip(), limit=limit)
        if not matched:
            return []
        # Build enriched results with real knowledge entry data
        all_entries_data = service.list_skills(limit=500)
        entry_map = {e["id"]: e for e in all_entries_data}
        results: list[dict[str, Any]] = []
        for entry in matched:
            entry_data = entry_map.get(entry.id, {})
            # Compute a simple relevance score based on token overlap
            tokens = [t.lower() for t in q.split() if len(t) > 1]
            text = " ".join(
                [entry.name, entry.description, entry.applicable_context]
                + entry.triggers + entry.keywords + entry.error_types
            ).lower()
            score = min(1.0, sum(text.count(t) for t in tokens) / max(len(tokens) * 3, 1))
            matched_kws = [kw for kw in entry.keywords + entry.triggers if any(t in kw.lower() for t in tokens)]
            results.append({
                "entry": entry_data or {
                    "id": entry.id, "name": entry.name, "description": entry.description,
                    "hit_rate": entry.hit_rate, "success_rate": entry.success_rate,
                    "usage_count": entry.usage_count, "last_used_at": entry.last_used_at or "",
                    "triggers": entry.triggers, "error_types": entry.error_types,
                    "action_template": entry.action_template, "keywords": entry.keywords,
                    "applicable_context": entry.applicable_context or "",
                    "related_bug_ids": [], "status": entry.status,
                    "status_label": {"active": "生效", "needs_optimization": "待优化", "retired": "已下架"}.get(entry.status, entry.status),
                },
                "score": round(score, 3),
                "match_type": "bm25",
                "matched_keywords": matched_kws[:8],
            })
        return results

    @app.get("/api/knowledge/{entry_id}")
    def knowledge_detail(entry_id: str) -> dict[str, Any]:
        try:
            return service.get_skill_detail(entry_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/knowledge/{entry_id}")
    def update_knowledge(entry_id: str, payload: KnowledgeUpsertRequest) -> dict[str, Any]:
        try:
            return service.update_skill(entry_id, payload.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/knowledge/{entry_id}/retire")
    def retire_knowledge(entry_id: str, payload: KnowledgeRetireRequest) -> dict[str, Any]:
        try:
            return service.retire_skill(entry_id, payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/knowledge/{entry_id}/restore")
    def restore_knowledge(entry_id: str) -> dict[str, Any]:
        try:
            return service.restore_skill(entry_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # --- Review Actions ---

    @app.post("/api/reviews/{bug_report_id}/approve")
    def approve_review(bug_report_id: str, payload: ReviewActionRequest | None = None) -> dict[str, Any]:
        notes = (payload and payload.notes) or ""
        success, msg = review_workflow.approve(bug_report_id, reviewer="admin", notes=notes)
        new_status = "approved" if success else review_workflow.get_current_status(bug_report_id).value
        return {"success": success, "message": msg, "bug_report_id": bug_report_id, "new_status": new_status}

    @app.post("/api/reviews/{bug_report_id}/reject")
    def reject_review(bug_report_id: str, payload: ReviewActionRequest) -> dict[str, Any]:
        notes = (payload and payload.notes) or ""
        success, msg = review_workflow.reject(bug_report_id, reviewer="admin", notes=notes)
        new_status = "rejected" if success else review_workflow.get_current_status(bug_report_id).value
        return {"success": success, "message": msg, "bug_report_id": bug_report_id, "new_status": new_status}

    @app.post("/api/reviews/{bug_report_id}/publish")
    def publish_review(bug_report_id: str) -> dict[str, Any]:
        try:
            report = service.get_incident_detail(bug_report_id)
        except KeyError:
            wiki_url = None
        else:
            doc = report.get("review_document") or {}
            if isinstance(doc, str):
                import json as _json
                try:
                    doc = _json.loads(doc)
                except _json.JSONDecodeError:
                    doc = {}
            wiki_result = wiki_publisher.publish(
                bug_report_id=bug_report_id,
                title=str(report.get("title", "Bug Review")),
                content=doc,
                repo_name=str(report.get("repo_name", "")),
                keywords=doc.get("keywords", []),
            )
            wiki_url = str(wiki_result.get("url", ""))

        success, msg = review_workflow.publish(bug_report_id, reviewer="system")
        new_status = "published" if success else review_workflow.get_current_status(bug_report_id).value
        return {"success": success, "message": msg, "bug_report_id": bug_report_id,
                "new_status": new_status, "wiki_url": wiki_url or ""}

    @app.get("/api/reviews/{bug_report_id}/history")
    def review_history(bug_report_id: str) -> list[dict[str, Any]]:
        return review_workflow.get_history(bug_report_id)

    # --- Webhooks ---

    @app.post("/webhooks/git")
    async def git_webhook(request: Request) -> dict[str, Any]:
        body = await request.json()
        github_event = request.headers.get("X-GitHub-Event", "")
        gitlab_event = request.headers.get("X-Gitlab-Event", "")

        bug_report: Any = None
        if github_event:
            # signature verification (optional, skip if no secret configured)
            _ = request.headers.get("X-Hub-Signature-256", "")
            bug_report = pr_listener.parse_github_event(
                event_type=github_event, payload=body,
            )
        elif gitlab_event:
            bug_report = pr_listener.parse_gitlab_event(
                event_type=gitlab_event, payload=body,
            )

        if bug_report is None:
            return {"status": "ignored", "reason": "not_a_bug_fix_pr"}
        return {"status": "accepted", "bug_report": bug_report}

    @app.post("/webhooks/manual")
    def manual_webhook(payload: WebhookManualRequest) -> dict[str, Any]:
        bug_report = pr_listener.parse_manual_input(
            title=payload.pr_title or "",
            description=payload.description or "",
            logs=payload.logs or "",
            pr_url=payload.pr_url or "",
            metadata={"repo_id": payload.repo_id},
        )
        data = {
            "repo_id": payload.repo_id,
            "title": bug_report.title,
            "description": bug_report.description,
            "source_type": "manual",
            "logs_excerpt": payload.logs or "",
            "bug_category": bug_report.metadata.get("bug_category", "unknown") if bug_report.metadata else "unknown",
        }
        result = service.create_incident(payload.repo_id, data)
        return {"status": "created", "bug_report": result}

    # --- Data Source Webhook ---

    @app.get("/api/repos/{repo_id}/webhook")
    def repo_webhook_info(repo_id: str) -> dict[str, Any]:
        try:
            repo = service.get_repository_detail(repo_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Repository not found")
        base = str(repo.get("url", "")).rstrip("/")
        return {
            "url": f"{base}/settings/hooks" if base else "(configure manually)",
            "secret": "PR-LISTENER-SECRET-PLACEHOLDER",
            "events": ["pull_request", "push", "workflow_run"],
        }

    @app.post("/api/repos/{repo_id}/webhook/toggle")
    def toggle_repo_webhook(repo_id: str, payload: dict[str, Any] = None) -> dict[str, Any]:
        _ = payload or {}
        return {"success": True, "repo_id": repo_id, "message": "Webhook toggle is simulated (local dev mode)"}

    # --- Demo Reset ---

    @app.post("/api/demo/reset")
    def demo_reset() -> dict[str, Any]:
        """一键重置 Demo 数据，重新注入 gulugulu33/aiflow-studio 示例。"""
        import subprocess, sys as _sys
        demo_script = Path(__file__).resolve().parents[3] / "start_demo.py"
        if not demo_script.exists():
            raise HTTPException(status_code=404, detail="start_demo.py not found")
        try:
            # 只重新写入数据文件，不重启服务
            import importlib.util, types
            spec = importlib.util.spec_from_file_location("_demo_seed", demo_script)
            # 直接执行数据注入部分（不启动 uvicorn）
            src = demo_script.read_text()
            # 截断到 uvicorn.run 之前
            cutoff = src.find("import uvicorn")
            if cutoff > 0:
                src = src[:cutoff]
            exec(compile(src, str(demo_script), "exec"), {"__name__": "__demo_reset__"})  # noqa: S102
            return {"success": True, "message": "Demo 数据已重置，请刷新页面"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"重置失败：{exc}") from exc

    @app.get("/api/demo/info")
    def demo_info() -> dict[str, Any]:
        """返回当前 Demo 示例仓库信息。"""
        return {
            "repo": "gulugulu33/aiflow-studio",
            "repo_url": "https://github.com/gulugulu33/aiflow-studio",
            "description": "AI Flow Studio — 可视化 AI 工作流编排平台",
            "bug_reports": 4,
            "knowledge_entries": 5,
            "data_sources": 2,
            "flow": [
                {"step": 1, "label": "PR 接收", "desc": "GitHub Webhook 推送 PR 事件"},
                {"step": 2, "label": "LLM 抽取", "desc": "AI 自动提取根因/修复方案/预防措施"},
                {"step": 3, "label": "人工审核", "desc": "工程师审核 AI 抽取结果"},
                {"step": 4, "label": "发布知识库", "desc": "审核通过后沉淀为团队踩坑知识"},
            ],
        }

    return app


app = create_app()


def run_dev_server() -> None:
    import uvicorn

    uvicorn.run(
        "repo_maintainer.api:app",
        host=app.state.platform.config.api.host,
        port=app.state.platform.config.api.port,
        reload=False,
    )
