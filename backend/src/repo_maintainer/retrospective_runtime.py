"""Environment wiring. Production dependencies are mandatory when enabled."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .retrospective_connectors import (
    FeishuConnector,
    GitHubConnector,
    OpenAIEmbedder,
    OpenAIReviewGenerator,
    RedisTokenCache,
)
from .retrospective_pipeline import ContextSelector, RetrospectivePipeline
from .retrospective_code_context import GitHubArchiveSource, RepositoryCodeContext
from .retrospective_store import PostgresRetrospectiveStore, record_generation_context


class TaskQueue(Protocol):
    def enqueue(self, incident_id: str) -> str: ...


class InlineTaskQueue:
    """Explicit development mode only; never selected by the Docker production profile."""

    def __init__(self, pipeline: RetrospectivePipeline) -> None:
        self.pipeline = pipeline

    def enqueue(self, incident_id: str) -> str:
        self.pipeline.run(incident_id)
        return f"inline:{incident_id}"


class CeleryTaskQueue:
    def __init__(self, broker_url: str) -> None:
        try:
            from celery import Celery
        except ImportError as exc:  # pragma: no cover - exercised in container
            raise RuntimeError("Celery task queue requires the celery package") from exc
        self.client = Celery("retrospective-client", broker=broker_url)

    def enqueue(self, incident_id: str) -> str:
        result = self.client.send_task("repo_maintainer.run_retrospective", args=[incident_id])
        return str(result.id)


def create_pipeline_from_env(*, initialize_store: bool = False) -> RetrospectivePipeline:
    database_url = _required("DATABASE_URL")
    redis_url = _required("REDIS_URL")
    openai_api_key = _required("OPENAI_API_KEY")
    openai_model = _required("OPENAI_MODEL")
    embedding_model = _required("EMBEDDING_MODEL")
    github_token = _required("GITHUB_TOKEN")
    github_secret = _required("GITHUB_WEBHOOK_SECRET")
    feishu_app_id = _required("FEISHU_APP_ID")
    feishu_app_secret = _required("FEISHU_APP_SECRET")
    feishu_space_id = _required("FEISHU_WIKI_SPACE_ID")
    feishu_parent = _required("FEISHU_WIKI_PARENT_NODE_TOKEN")

    store = PostgresRetrospectiveStore(
        database_url,
        rrf_k=int(os.getenv("RAG_RRF_K", "60")),
        channel_pool=int(os.getenv("RAG_CHANNEL_POOL", "40")),
    )
    if initialize_store:
        store.initialize()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    cache = RedisTokenCache(redis_url)
    collaboration = FeishuConnector(
        app_id=feishu_app_id,
        app_secret=feishu_app_secret,
        cache=cache,
        base_url=os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn"),
        wiki_space_id=feishu_space_id,
        wiki_parent_node_token=feishu_parent,
    )
    generator = OpenAIReviewGenerator(
        api_key=openai_api_key, model=openai_model, base_url=base_url,
        timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS', '60')),
        enable_thinking=_optional_bool_env('OPENAI_ENABLE_THINKING'),
    )
    generator.discovery_recorder = lambda incident, request: record_generation_context(
        store, incident, 'code_discovery', request, scope='provider_request')
    tools_enabled = _optional_bool_env('REVIEW_TOOLS_ENABLED') is True
    code_context = None
    if os.getenv("CODE_CONTEXT_ENABLED", "1").strip().lower() not in {"0", "false", "no"}:
        code_context = RepositoryCodeContext(
            GitHubArchiveSource(
                github_token,
                api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"),
                max_archive_bytes=int(os.getenv("CODE_CONTEXT_MAX_ARCHIVE_BYTES", "30000000")),
            ),
            max_files=int(os.getenv("CODE_CONTEXT_MAX_FILES", "10")),
            max_expanded_files=int(os.getenv("CODE_CONTEXT_MAX_EXPANDED_FILES", "40")),
            planner=(generator.plan_code_context
                     if _optional_bool_env('CODE_CONTEXT_PLANNER_ENABLED') is True and not tools_enabled else None),
        )
    return RetrospectivePipeline(
        store=store,
        git=GitHubConnector(github_token, api_url=os.getenv("GITHUB_API_URL", "https://api.github.com")),
        code_context=code_context,
        collaboration=collaboration,
        generator=generator,
        embedder=OpenAIEmbedder(
            api_key=openai_api_key,
            model=embedding_model,
            base_url=base_url,
            timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60")),
        ),
        github_webhook_secret=github_secret,
        selector=ContextSelector(
            budget_chars=int(os.getenv("CONTEXT_BUDGET_CHARS", "24000")),
            max_item_chars=int(os.getenv("CONTEXT_ITEM_MAX_CHARS", "6000")),
        ),
        top_k=int(os.getenv("RAG_TOP_K", "5")),
        rerank_pool=int(os.getenv("RAG_RERANK_POOL", "20")),
        max_generation_attempts=int(os.getenv("LLM_MAX_RETRIES", "2")),
        default_feishu_chat_ids=_csv_env("FEISHU_CHAT_IDS"),
        default_feishu_doc_tokens=_csv_env("FEISHU_DOC_TOKENS"),
        tools_enabled=tools_enabled,
    )


def create_task_queue_from_env(pipeline: RetrospectivePipeline) -> TaskQueue:
    mode = os.getenv("TASK_QUEUE_MODE", "celery").strip().lower()
    if mode == "inline":
        return InlineTaskQueue(pipeline)
    if mode == "celery":
        return CeleryTaskQueue(_required("REDIS_URL"))
    raise ValueError(f"Unsupported TASK_QUEUE_MODE: {mode}")


def configure_app_pipeline(app) -> None:
    app.state.retrospective_pipeline = None
    app.state.retrospective_queue = None
    app.state.retrospective_error = ""
    if os.getenv("REVIEW_PIPELINE_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        pipeline = create_pipeline_from_env(initialize_store=False)
        app.state.retrospective_pipeline = pipeline
        app.state.retrospective_queue = create_task_queue_from_env(pipeline)
    except Exception as exc:  # fail readiness without breaking legacy demo endpoints
        app.state.retrospective_error = str(exc)


def _optional_bool_env(name: str) -> bool | None:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be blank, true or false")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when REVIEW_PIPELINE_ENABLED=1")
    return value


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]
