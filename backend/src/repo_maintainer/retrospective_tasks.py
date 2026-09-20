"""Celery worker entrypoint for the retrospective pipeline."""

from __future__ import annotations

import os

from celery import Celery

from .retrospective_runtime import create_pipeline_from_env


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
    autoretry_for=(TimeoutError, ConnectionError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def run_retrospective(self, incident_id: str) -> dict:
    pipeline = create_pipeline_from_env(initialize_store=False)
    job_id = str(self.request.id)
    pipeline.store.create_job(job_id, incident_id, "collecting", "running")
    try:
        version = pipeline.run(incident_id)
        pipeline.store.update_job(job_id, stage="pending_review", status="completed")
        return version.model_dump(mode="json")
    except Exception as exc:
        pipeline.store.update_job(job_id, stage="failed", status="failed", error=str(exc))
        raise
