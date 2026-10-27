"""Typed contracts for the department-level retrospective pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class IncidentStatus(str, Enum):
    RECEIVED = "received"
    COLLECTING = "collecting"
    RETRIEVING = "retrieving"
    GENERATING = "generating"
    VALIDATING = "validating"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


ALLOWED_STATUS_TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.RECEIVED: {IncidentStatus.COLLECTING, IncidentStatus.FAILED},
    IncidentStatus.COLLECTING: {IncidentStatus.RETRIEVING, IncidentStatus.FAILED},
    IncidentStatus.RETRIEVING: {IncidentStatus.GENERATING, IncidentStatus.FAILED},
    IncidentStatus.GENERATING: {IncidentStatus.VALIDATING, IncidentStatus.FAILED},
    IncidentStatus.VALIDATING: {IncidentStatus.PENDING_REVIEW, IncidentStatus.GENERATING, IncidentStatus.FAILED},
    IncidentStatus.PENDING_REVIEW: {IncidentStatus.REJECTED, IncidentStatus.APPROVED},
    IncidentStatus.REJECTED: {IncidentStatus.GENERATING, IncidentStatus.PENDING_REVIEW},
    IncidentStatus.APPROVED: {IncidentStatus.PUBLISHING},
    IncidentStatus.PUBLISHING: {IncidentStatus.PUBLISHED, IncidentStatus.APPROVED},
    IncidentStatus.PUBLISHED: set(),
    IncidentStatus.FAILED: {
        IncidentStatus.COLLECTING,
        IncidentStatus.RETRIEVING,
        IncidentStatus.GENERATING,
        IncidentStatus.PUBLISHING,
    },
}


class EvidenceSource(str, Enum):
    GITHUB_PR = "github_pr"
    GITHUB_COMMIT = "github_commit"
    GITHUB_DIFF = "github_diff"
    REPOSITORY_CODE = "repository_code"
    CI_LOG = "ci_log"
    FEISHU_MESSAGE = "feishu_message"
    FEISHU_DOC = "feishu_doc"
    HISTORICAL = "historical"
    MANUAL = "manual"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("ev"))
    incident_id: str
    source_type: EvidenceSource
    content: str = Field(min_length=1)
    location: str
    source_url: str = ""
    repo_id: str = ""
    commit_sha: str = ""
    occurred_at: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    selected: bool = False
    selection_reason: str = ""


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    quote: str = Field(min_length=1)


class EvidenceBackedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)


class ImpactSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1)
    severity: Literal["P0", "P1", "P2", "P3"] = "P2"
    affected_users: str | None = None
    citations: list[Citation] = Field(min_length=1)


class RetrospectiveDocument(BaseModel):
    """Strict LLM output. Core factual conclusions must cite stored evidence."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    impact: ImpactSection
    root_cause: EvidenceBackedText
    fix_solution: EvidenceBackedText
    prevention: list[str] = Field(min_length=1)
    related_modules: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_errors: list[str] = Field(default_factory=list)
    soft_warnings: list[str] = Field(default_factory=list)
    field_completeness: float = 0.0
    evidence_coverage: float = 0.0
    citation_accuracy: float = 0.0
    score: float = 0.0

    @property
    def hard_passed(self) -> bool:
        return not self.hard_errors


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = ""
    repo_id: str = ""
    repo_name: str = ""
    incident_type: str = "unknown"
    source_type: str = "manual"
    pr_number: int | None = None
    commit_sha: str = ""
    source_url: str = ""
    logs: str = ""
    feishu_chat_ids: list[str] = Field(default_factory=list)
    feishu_doc_tokens: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentRecord(IncidentCreate):
    id: str = Field(default_factory=lambda: new_id("inc"))
    status: IncidentStatus = IncidentStatus.RECEIVED
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    current_version: int = 0
    retry_count: int = 0
    failed_stage: str = ""
    error: str = ""
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class RetrospectiveVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("retro"))
    incident_id: str
    version: int
    kind: Literal["ai", "human"] = "ai"
    document: RetrospectiveDocument
    quality: QualityReport
    evidence_ids: list[str] = Field(default_factory=list)
    model: str = ""
    prompt_version: str = "v1"
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    actor: str = "system"
    created_at: str = Field(default_factory=utc_now)


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("review"))
    incident_id: str
    action: Literal["edit", "submit", "approve", "reject", "publish"]
    actor: str
    reason: str = ""
    from_status: IncidentStatus
    to_status: IncidentStatus
    created_at: str = Field(default_factory=utc_now)


class PublishResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_id: str
    url: str
    created: bool = True


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_id: str
    incident_id: str
    title: str
    content: str
    score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0
    source_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class GenerationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class GenerationContext(BaseModel):
    """Immutable input saved before each generation call, including failed calls."""

    id: str = Field(default_factory=lambda: new_id('ctx'))
    incident_id: str
    payload: dict[str, Any]
    content_hash: str
    created_at: str = Field(default_factory=utc_now)
