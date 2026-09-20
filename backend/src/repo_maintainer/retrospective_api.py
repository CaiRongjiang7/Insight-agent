"""FastAPI routes for the new evidence-backed retrospective pipeline."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .retrospective_models import IncidentCreate, RetrospectiveDocument
from .retrospective_pipeline import PipelineError, RetrospectivePipeline


class ReviewCommand(BaseModel):
    actor: str = "reviewer"
    reason: str = ""


class EditCommand(BaseModel):
    actor: str = "reviewer"
    document: RetrospectiveDocument


def mount_retrospective_api(app: FastAPI) -> None:
    def pipeline(request: Request) -> RetrospectivePipeline:
        value = getattr(request.app.state, "retrospective_pipeline", None)
        if value is None:
            detail = getattr(request.app.state, "retrospective_error", "") or "retrospective pipeline is disabled"
            raise HTTPException(status_code=503, detail=detail)
        return value

    def as_http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, PipelineError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/connectors/status")
    def connector_status(request: Request) -> dict[str, Any]:
        value = getattr(request.app.state, "retrospective_pipeline", None)
        if value is None:
            return {
                "ready": False,
                "error": getattr(request.app.state, "retrospective_error", "") or "retrospective pipeline is disabled",
            }
        return {"ready": True, **value.connector_status()}

    @app.post("/api/retrospective/incidents", status_code=201)
    def create_incident(payload: IncidentCreate, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        incident = value.create_incident(payload)
        return incident.model_dump(mode="json")

    @app.get("/api/retrospective/incidents")
    def list_incidents(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        value = pipeline(request)
        return [item.model_dump(mode="json") for item in value.store.list_incidents(limit)]

    @app.get("/api/retrospective/incidents/{incident_id}")
    def incident_detail(incident_id: str, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            incident = value.store.get_incident(incident_id)
        except Exception as exc:
            raise as_http_error(exc) from exc
        latest = value.store.latest_version(incident_id)
        return {
            "incident": incident.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in value.store.list_evidence(incident_id)],
            "retrospective": latest.model_dump(mode="json") if latest else None,
            "versions": [item.model_dump(mode="json") for item in value.store.list_versions(incident_id)],
            "review_actions": [item.model_dump(mode="json") for item in value.store.list_review_actions(incident_id)],
        }

    @app.post("/api/retrospective/incidents/{incident_id}/run", status_code=202)
    def run_incident(incident_id: str, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        queue = getattr(request.app.state, "retrospective_queue", None)
        if queue is None:
            raise HTTPException(status_code=503, detail="retrospective task queue is unavailable")
        try:
            value.store.get_incident(incident_id)
            job_id = queue.enqueue(incident_id)
            value.store.create_job(job_id, incident_id, "collecting", "queued")
            return {"incident_id": incident_id, "job_id": job_id, "accepted": True}
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.post("/api/retrospective/incidents/{incident_id}/retry")
    def retry_incident(incident_id: str, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            version = value.retry(incident_id)
            return version.model_dump(mode="json")
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.post("/api/retrospective/incidents/{incident_id}/regenerate")
    def regenerate_incident(incident_id: str, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            version = value.regenerate(incident_id)
            return version.model_dump(mode="json")
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.patch("/api/retrospective/incidents/{incident_id}/document")
    def edit_document(incident_id: str, payload: EditCommand, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            version = value.edit(incident_id, payload.document, payload.actor)
            return version.model_dump(mode="json")
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.post("/api/retrospective/incidents/{incident_id}/approve")
    def approve(incident_id: str, payload: ReviewCommand, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            return value.approve(incident_id, payload.actor, payload.reason).model_dump(mode="json")
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.post("/api/retrospective/incidents/{incident_id}/reject")
    def reject(incident_id: str, payload: ReviewCommand, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            return value.reject(incident_id, payload.actor, payload.reason).model_dump(mode="json")
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.post("/api/retrospective/incidents/{incident_id}/publish")
    def publish(incident_id: str, payload: ReviewCommand, request: Request) -> dict[str, Any]:
        value = pipeline(request)
        try:
            result = value.publish(incident_id, payload.actor)
            publication = result["publication"]
            return {**result, "publication": publication.model_dump(mode="json")}
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.get("/api/retrospective/knowledge/search")
    def search_knowledge(
        request: Request,
        q: str = Query(min_length=1),
        repo_id: str = "",
        incident_type: str = "",
        limit: int = Query(default=5, ge=1, le=20),
    ) -> list[dict[str, Any]]:
        value = pipeline(request)
        try:
            return [
                item.model_dump(mode="json")
                for item in value.search_knowledge(q, repo_id=repo_id, incident_type=incident_type, limit=limit)
            ]
        except Exception as exc:
            raise as_http_error(exc) from exc

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> dict[str, Any]:
        value = pipeline(request)
        raw_body = await request.body()
        try:
            incident, duplicate = value.ingest_github_webhook(
                delivery_id=request.headers.get("X-GitHub-Delivery", ""),
                event_type=request.headers.get("X-GitHub-Event", ""),
                signature=request.headers.get("X-Hub-Signature-256", ""),
                raw_body=raw_body,
            )
            if incident is None:
                return {"status": "ignored", "duplicate": duplicate}
            if duplicate:
                return {"status": "duplicate", "incident_id": incident.id, "duplicate": True}
            queue = getattr(request.app.state, "retrospective_queue", None)
            if queue is None:
                raise PipelineError("retrospective task queue is unavailable")
            job_id = queue.enqueue(incident.id)
            value.store.create_job(job_id, incident.id, "collecting", "queued")
            return {"status": "accepted", "incident_id": incident.id, "job_id": job_id, "duplicate": False}
        except Exception as exc:
            if "signature" in str(exc).lower():
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            raise as_http_error(exc) from exc
