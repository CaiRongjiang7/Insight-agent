"""Celery worker entrypoint for the retrospective pipeline."""

from __future__ import annotations

import os

from celery import Celery
from psycopg import InterfaceError, OperationalError

from .retrospective_runtime import create_pipeline_from_env
from .retrospective_feishu_bot import FeishuIncidentBusy, FeishuNotificationError, process_feishu_event
from .retrospective_models import IncidentStatus


redis_url = os.environ["REDIS_URL"]
celery_app = Celery("repo_maintainer", broker=redis_url, backend=redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)


@celery_app.task(
    bind=True,
    name="repo_maintainer.run_retrospective",
    autoretry_for=(TimeoutError, ConnectionError, FeishuIncidentBusy),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def run_retrospective(self, incident_id: str) -> dict:
    pipeline = create_pipeline_from_env(initialize_store=False)
    job_id = str(self.request.id)
    with pipeline.store.execution_lock(incident_id) as acquired:
        if not acquired:
            raise FeishuIncidentBusy(incident_id)
        incident = pipeline.store.get_incident(incident_id)
        if incident.status not in {IncidentStatus.RECEIVED, IncidentStatus.FAILED}:
            pipeline.store.create_job(job_id, incident_id, incident.status.value, "skipped")
            pipeline.store.update_job(job_id, stage=incident.status.value, status="skipped")
            return {"incident_id": incident_id, "status": "skipped"}
        return _run_retrospective(pipeline, job_id, incident_id)


def _run_retrospective(pipeline, job_id: str, incident_id: str) -> dict:
    pipeline.store.create_job(job_id, incident_id, "collecting", "running")
    pipeline.store.update_job(job_id, stage="collecting", status="running")
    try:
        version = pipeline.run(incident_id)
        pipeline.store.update_job(job_id, stage="pending_review", status="completed")
        return version.model_dump(mode="json")
    except Exception as exc:
        pipeline.store.update_job(job_id, stage="failed", status="failed", error=str(exc))
        raise


@celery_app.task(bind=True, name="repo_maintainer.revise_retrospective",
                 autoretry_for=(FeishuIncidentBusy, OperationalError, InterfaceError),
                 retry_backoff=True, retry_kwargs={"max_retries": 5})
def revise_retrospective(self, incident_id: str, version_id: str) -> dict:
    pipeline = create_pipeline_from_env(initialize_store=False)
    with pipeline.store.execution_lock(incident_id) as acquired:
        if not acquired:
            raise FeishuIncidentBusy(incident_id)
        incident = pipeline.store.get_incident(incident_id)
        version = pipeline.store.latest_version(incident_id)
        job_id = str(self.request.id)
        if incident.status != IncidentStatus.REJECTED or version is None or version.id != version_id:
            pipeline.store.create_job(job_id, incident_id, incident.status.value, "skipped")
            pipeline.store.update_job(job_id, stage=incident.status.value, status="skipped")
            _revision_status(pipeline, incident_id, job_id, "skipped")
            return {"incident_id": incident_id, "status": "skipped"}
        _revision_status(pipeline, incident_id, job_id, "running")
        try:
            result = _run_retrospective(pipeline, job_id, incident_id)
        except Exception:
            _revision_status(pipeline, incident_id, job_id, "failed")
            raise
        _revision_status(pipeline, incident_id, job_id, "completed")
        return result


def _revision_status(pipeline, incident_id, job_id, status):
    incident = pipeline.store.get_incident(incident_id)
    # Do not overwrite the status of a newer queued request.
    if incident.metadata.get("revision_job_id") in {None, job_id}:
        pipeline.store.update_incident(incident_id, metadata={**incident.metadata,
                                      "revision_job_id": job_id, "revision_job_status": status})


@celery_app.task(name="repo_maintainer.notify_retrospective_review",
                 autoretry_for=(RuntimeError, OperationalError, InterfaceError),
                 retry_backoff=True, retry_backoff_max=60, retry_kwargs={"max_retries": 5})
def notify_retrospective_review(incident_id: str, version_id: str) -> dict:
    pipeline = create_pipeline_from_env(initialize_store=False)
    with pipeline.store.execution_lock(incident_id) as acquired:
        if not acquired:
            raise FeishuIncidentBusy(incident_id)
        return pipeline.review_notifier.notify(incident_id, version_id)


@celery_app.task(
    bind=True, name="repo_maintainer.handle_feishu_command", reject_on_worker_lost=True,
    autoretry_for=(FeishuNotificationError, FeishuIncidentBusy, OperationalError, InterfaceError),
    retry_backoff=True, retry_backoff_max=60, retry_kwargs={"max_retries": 5},
)
def handle_feishu_command(self, payload: dict, bot_open_id: str) -> dict:
    pipeline = create_pipeline_from_env(initialize_store=False)
    return process_feishu_event(payload, bot_open_id=bot_open_id,
        app_id=os.environ["FEISHU_APP_ID"],
        allowed_chat_ids={item.strip() for item in os.getenv("FEISHU_CHAT_IDS", "").split(",") if item.strip()},
        pipeline=pipeline, job_id=str(self.request.id),
        public_base_url=os.getenv("REVIEW_PUBLIC_BASE_URL", ""))
