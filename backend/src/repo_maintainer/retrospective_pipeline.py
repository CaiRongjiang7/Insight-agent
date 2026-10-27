"""Deterministic orchestration for the end-to-end retrospective knowledge loop."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from .retrospective_connectors import CollaborationConnector, Embedder, GitContextConnector, ReviewGenerator
from .retrospective_code_context import RepositoryCodeContext, RepositoryContextError
from .retrospective_models import (
    ALLOWED_STATUS_TRANSITIONS,
    Citation,
    Evidence,
    EvidenceSource,
    IncidentCreate,
    IncidentRecord,
    IncidentStatus,
    KnowledgeHit,
    PublishResult,
    QualityReport,
    RetrospectiveDocument,
    RetrospectiveVersion,
    ReviewAction,
    new_id,
)
from .retrospective_store import RetrospectiveStore, record_generation_context


class PipelineError(RuntimeError):
    pass


@dataclass(slots=True)
class ContextSelection:
    evidence: list[Evidence]
    used_chars: int
    budget_chars: int


class ContextSelector:
    """Preserve raw evidence in storage while sending only relevant excerpts."""

    SOURCE_PRIORITY = {
        EvidenceSource.CI_LOG: 100,
        EvidenceSource.GITHUB_DIFF: 95,
        EvidenceSource.REPOSITORY_CODE: 93,
        EvidenceSource.GITHUB_PR: 90,
        EvidenceSource.GITHUB_COMMIT: 85,
        EvidenceSource.FEISHU_MESSAGE: 75,
        EvidenceSource.FEISHU_DOC: 60,
        EvidenceSource.MANUAL: 90,
        EvidenceSource.HISTORICAL: 40,
    }

    def __init__(self, budget_chars: int = 24_000, max_item_chars: int = 6_000) -> None:
        self.budget_chars = max(budget_chars, 2_000)
        self.max_item_chars = max(max_item_chars, 500)

    def select(self, incident: IncidentRecord, evidence: list[Evidence]) -> ContextSelection:
        query_terms = set(_tokens(" ".join([incident.title, incident.description, incident.incident_type, incident.logs])))
        ranked: list[tuple[float, Evidence, str]] = []
        seen: set[str] = set()
        for item in evidence:
            normalized = " ".join(item.content.split())
            if item.source_type == EvidenceSource.REPOSITORY_CODE:
                normalized = f"{item.location} {normalized}"
            fingerprint = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            content_terms = set(_tokens(f"{item.location} {item.content}"))
            overlap = len(query_terms & content_terms)
            priority = float(self.SOURCE_PRIORITY.get(item.source_type, 50))
            score = priority + min(overlap * 4, 40)
            if item.source_type == EvidenceSource.REPOSITORY_CODE:
                score += min(float(item.metadata.get("retrieval_score", 0)) / 10, 20)
            reason = f"source_priority={int(priority)}, keyword_overlap={overlap}"
            processed = item.model_copy(deep=True)
            processed.content = self._prepare_content(processed, query_terms)
            processed.selection_reason = reason
            ranked.append((score, processed, reason))
        ranked.sort(key=lambda row: (-row[0], row[1].id))
        selected: list[Evidence] = []
        used = 0
        selected_ids: set[str] = set()

        def include(item: Evidence, reason: str, cap: int) -> None:
            nonlocal used
            remaining = self.budget_chars - used
            allowed = min(remaining, cap)
            if allowed <= 0 or (allowed < 300 and len(item.content) > allowed):
                return
            if len(item.content) > allowed:
                if item.source_type == EvidenceSource.REPOSITORY_CODE:
                    item.metadata['region_complete'] = False
                if item.source_type in {EvidenceSource.CI_LOG, EvidenceSource.GITHUB_DIFF} and allowed > 200:
                    marker = "\n...<context clipped>...\n"
                    head = (allowed - len(marker)) // 2
                    item.content = item.content[:head] + marker + item.content[-(allowed - len(marker) - head) :]
                else:
                    item.content = item.content[:allowed]
                reason += ", clipped_to_budget"
            item.selected = True
            item.selection_reason = reason
            selected.append(item)
            selected_ids.add(item.id)
            used += len(item.content)

        # Preserve several kinds of direct evidence before large logs consume the budget.
        diverse_sources = (
            EvidenceSource.CI_LOG,
            EvidenceSource.GITHUB_DIFF,
            EvidenceSource.REPOSITORY_CODE,
            EvidenceSource.FEISHU_MESSAGE,
            EvidenceSource.FEISHU_DOC,
            EvidenceSource.GITHUB_PR,
            EvidenceSource.GITHUB_COMMIT,
        )
        first_item_cap = min(self.max_item_chars, max(300, self.budget_chars // len(diverse_sources)))
        for source in diverse_sources:
            source_items = [(item, reason) for _, item, reason in ranked if item.source_type == source]
            if source == EvidenceSource.REPOSITORY_CODE:
                related = [
                    pair for pair in source_items
                    if "changed_file" not in pair[0].metadata.get("relation", [])
                ]
                first = (related or source_items)[0] if source_items else None
            else:
                first = source_items[0] if source_items else None
            if first is not None:
                include(first[0], first[1] + ", source_diversity", first_item_cap)

        # Spread the code share across changed files before high-ranking logs or
        # multiple regions from one file use the remaining prompt budget.
        changed_code: list[tuple[Evidence, str]] = []
        covered_paths: set[str] = {
            str(item.metadata.get("file_path") or item.location.split("#L", 1)[0])
            for item in selected
            if item.source_type == EvidenceSource.REPOSITORY_CODE
            and "changed_file" in item.metadata.get("relation", [])
        }
        for _, item, reason in ranked:
            if item.source_type != EvidenceSource.REPOSITORY_CODE or "changed_file" not in item.metadata.get("relation", []):
                continue
            path = str(item.metadata.get("file_path") or item.location.split("#L", 1)[0])
            if path not in covered_paths and item.id not in selected_ids:
                changed_code.append((item, reason))
                covered_paths.add(path)
        code_budget = self.budget_chars // 2
        code_used = sum(len(item.content) for item in selected if item.source_type == EvidenceSource.REPOSITORY_CODE)
        if changed_code and code_budget - code_used >= 300:
            per_file_cap = max(300, min(self.max_item_chars, (code_budget - code_used) // len(changed_code)))
            for item, reason in changed_code:
                remaining_code = code_budget - code_used
                if remaining_code < 300:
                    break
                before = used
                include(item, reason + ", changed_file_coverage", min(per_file_cap, remaining_code))
                code_used += used - before
        for _, item, reason in ranked:
            if item.id not in selected_ids:
                include(item, reason, self.max_item_chars)
        return ContextSelection(evidence=selected, used_chars=used, budget_chars=self.budget_chars)

    def _prepare_content(self, evidence: Evidence, query_terms: set[str]) -> str:
        content = evidence.content
        if evidence.source_type == EvidenceSource.CI_LOG:
            content = _select_log_lines(content, query_terms)
        elif evidence.source_type == EvidenceSource.GITHUB_DIFF:
            content = _select_diff_hunks(content, query_terms)
        if len(content) <= self.max_item_chars:
            return content
        if evidence.source_type == EvidenceSource.REPOSITORY_CODE:
            evidence.metadata['region_complete'] = False
        head = self.max_item_chars // 2
        tail = self.max_item_chars - head
        return f"{content[:head]}\n...<context clipped>...\n{content[-tail:]}"


class QualityValidator:
    """Hard evidence checks plus non-blocking quality warnings."""

    def validate(
        self,
        document: RetrospectiveDocument,
        evidence: list[Evidence],
        history: list[KnowledgeHit],
        raw_evidence: list[Evidence] | None = None,
    ) -> QualityReport:
        evidence_map = {item.id: item for item in evidence}
        raw_map = {item.id: item for item in raw_evidence or evidence}
        hard_errors: list[str] = []
        citations = [
            *(document.impact.citations or []),
            *(document.root_cause.citations or []),
            *(document.fix_solution.citations or []),
        ]
        valid_count = 0
        for citation in citations:
            source = evidence_map.get(citation.evidence_id)
            if source is None:
                hard_errors.append(f"Unknown evidence ID: {citation.evidence_id}")
                continue
            if _normalize_quote(citation.quote) not in _normalize_quote(source.content):
                hard_errors.append(f"Quote not found in evidence {citation.evidence_id}")
                continue
            raw_source = raw_map.get(citation.evidence_id)
            if raw_source is None or _normalize_quote(citation.quote) not in _normalize_quote(raw_source.content):
                hard_errors.append(f"Quote not found in raw evidence {citation.evidence_id}")
                continue
            valid_count += 1
        section_count = 3
        covered = sum(
            bool(value)
            for value in [document.impact.citations, document.root_cause.citations, document.fix_solution.citations]
        )
        if covered < section_count:
            hard_errors.append("Impact, root cause and fix solution must each cite evidence")
        soft_warnings: list[str] = []
        if not document.related_modules:
            soft_warnings.append("No related modules identified")
        if len(document.keywords) < 2:
            soft_warnings.append("Fewer than two retrieval keywords")
        if not history:
            soft_warnings.append("No historical case was retrieved")
        completeness = sum(
            bool(value)
            for value in [
                document.title,
                document.summary,
                document.impact.scope,
                document.root_cause.content,
                document.fix_solution.content,
                document.prevention,
                document.related_modules,
                document.keywords,
            ]
        ) / 8
        coverage = covered / section_count
        accuracy = valid_count / max(len(citations), 1)
        score = (0.3 * completeness) + (0.35 * coverage) + (0.35 * accuracy)
        return QualityReport(
            hard_errors=hard_errors,
            soft_warnings=soft_warnings,
            field_completeness=round(completeness, 4),
            evidence_coverage=round(coverage, 4),
            citation_accuracy=round(accuracy, 4),
            score=round(score, 4),
        )


class RetrospectivePipeline:
    def __init__(
        self,
        *,
        store: RetrospectiveStore,
        git: GitContextConnector,
        collaboration: CollaborationConnector,
        generator: ReviewGenerator,
        embedder: Embedder,
        github_webhook_secret: str = "",
        selector: ContextSelector | None = None,
        validator: QualityValidator | None = None,
        top_k: int = 5,
        rerank_pool: int = 20,
        code_context: RepositoryCodeContext | None = None,
        max_generation_attempts: int = 2,
        default_feishu_chat_ids: list[str] | None = None,
        default_feishu_doc_tokens: list[str] | None = None,
    ) -> None:
        self.store = store
        self.git = git
        self.collaboration = collaboration
        self.generator = generator
        self.embedder = embedder
        self.github_webhook_secret = github_webhook_secret
        self.selector = selector or ContextSelector()
        self.validator = validator or QualityValidator()
        self.top_k = max(1, min(top_k, 20))
        self.rerank_pool = max(self.top_k, rerank_pool)
        self.code_context = code_context
        self.max_generation_attempts = max(max_generation_attempts, 1)
        self.default_feishu_chat_ids = default_feishu_chat_ids or []
        self.default_feishu_doc_tokens = default_feishu_doc_tokens or []

    def create_incident(self, payload: IncidentCreate) -> IncidentRecord:
        data = payload.model_dump()
        if not data["feishu_chat_ids"]:
            data["feishu_chat_ids"] = list(self.default_feishu_chat_ids)
        if not data["feishu_doc_tokens"]:
            data["feishu_doc_tokens"] = list(self.default_feishu_doc_tokens)
        return self.store.create_incident(IncidentRecord(**data))

    def _generate(self, incident, evidence, history):
        build_request = getattr(self.generator, 'build_request', None)
        generate_request = getattr(self.generator, 'generate_request', None)
        if callable(build_request) and callable(generate_request):
            request = build_request(incident, evidence, history)
            record_generation_context(self.store, incident, 'retrospective', request,
                                      scope='provider_request', evidence_ids=[e.id for e in evidence])
            return generate_request(request)
        record_generation_context(self.store, incident, 'retrospective', {
            'incident': incident.model_dump(mode='json'),
            'evidence': [e.model_dump(mode='json') for e in evidence],
            'history': [h.model_dump(mode='json') for h in history],
        }, scope='generator_arguments')
        return self.generator.generate(incident, evidence, history)

    def ingest_github_webhook(
        self,
        *,
        delivery_id: str,
        event_type: str,
        signature: str,
        raw_body: bytes,
    ) -> tuple[IncidentRecord | None, bool]:
        if not delivery_id:
            raise PipelineError("X-GitHub-Delivery is required")
        self._verify_github_signature(raw_body, signature)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError("Invalid GitHub webhook JSON") from exc
        if event_type != "pull_request":
            return None, False
        pr = payload.get("pull_request") or {}
        if payload.get("action") != "closed" or not bool(pr.get("merged")):
            return None, False
        if not self.store.register_source_event(delivery_id, event_type, {"action": payload.get("action")}):
            repo_name = str((payload.get("repository") or {}).get("full_name") or "unknown")
            incident_id = _github_incident_id(repo_name, int(pr.get("number") or payload.get("number") or 0))
            try:
                return self.store.get_incident(incident_id), True
            except KeyError:
                return None, True
        repo = payload.get("repository") or {}
        repo_name = str(repo.get("full_name") or "")
        pr_number = int(pr.get("number") or payload.get("number") or 0)
        incident = IncidentRecord(
            id=_github_incident_id(repo_name, pr_number),
            title=str(pr.get("title") or f"PR #{pr_number}"),
            description=str(pr.get("body") or ""),
            repo_id=repo_name,
            repo_name=repo_name,
            incident_type="bug_fix",
            source_type="github_pr",
            pr_number=pr_number,
            commit_sha=str((pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha") or "")),
            source_url=str(pr.get("html_url") or ""),
            metadata={
                "delivery_id": delivery_id,
                "merged_at": pr.get("merged_at"),
                "author": (pr.get("user") or {}).get("login", ""),
                "head_sha": (pr.get("head") or {}).get("sha", ""),
                "base_sha": (pr.get("base") or {}).get("sha", ""),
            },
            feishu_chat_ids=list(self.default_feishu_chat_ids),
            feishu_doc_tokens=list(self.default_feishu_doc_tokens),
        )
        return self.store.create_incident(incident), False

    def run(self, incident_id: str) -> RetrospectiveVersion:
        incident = self.store.get_incident(incident_id)
        try:
            self._transition(incident, IncidentStatus.COLLECTING)
            incident = self.store.get_incident(incident_id)
            raw_evidence = self.git.collect(incident) + self.collaboration.collect(incident)
            if self.code_context is not None and incident.repo_name and incident.commit_sha:
                try:
                    raw_evidence.extend(self.code_context.collect(incident, raw_evidence))
                except RepositoryContextError as exc:
                    self.store.update_incident(
                        incident_id,
                        metadata={**incident.metadata, "code_context_warning": str(exc)[:300]},
                    )
            if not raw_evidence:
                raise PipelineError("No source evidence was collected")
            self.store.add_evidence(raw_evidence)
            selection = self.selector.select(incident, raw_evidence)
            if not selection.evidence:
                raise PipelineError("No evidence remained after context selection")
            self.store.add_evidence(selection.evidence)

            self._transition(self.store.get_incident(incident_id), IncidentStatus.RETRIEVING)
            query = _retrieval_query(incident, selection.evidence)
            vectors = self.embedder.embed_texts([query, *[item.content for item in selection.evidence]])
            query_vector = vectors[0]
            self.store.index_evidence(selection.evidence, vectors[1:])
            history = self._retrieve_historical_cases(
                query,
                query_vector,
                repo_id=incident.repo_id,
                incident_type=incident.incident_type,
                limit=self.top_k,
            )

            last_quality: QualityReport | None = None
            last_document: RetrospectiveDocument | None = None
            last_metadata = None
            for attempt in range(self.max_generation_attempts):
                current = self.store.get_incident(incident_id)
                if current.status != IncidentStatus.GENERATING:
                    self._transition(current, IncidentStatus.GENERATING)
                generation_incident = incident
                if last_quality is not None:
                    generation_incident = incident.model_copy(update={
                        "metadata": {**incident.metadata, "generation_validation_errors": last_quality.hard_errors},
                    })
                document, metadata = self._generate(generation_incident, selection.evidence, history)
                self._transition(self.store.get_incident(incident_id), IncidentStatus.VALIDATING)
                quality = self.validator.validate(document, selection.evidence, history, raw_evidence)
                last_quality, last_document, last_metadata = quality, document, metadata
                if quality.hard_passed:
                    break
                if attempt + 1 < self.max_generation_attempts:
                    self._transition(self.store.get_incident(incident_id), IncidentStatus.GENERATING)
            if last_quality is None or last_document is None or last_metadata is None:
                raise PipelineError("Generator did not return a retrospective")
            if not last_quality.hard_passed:
                raise PipelineError("; ".join(last_quality.hard_errors))

            existing = self.store.list_versions(incident_id)
            version = RetrospectiveVersion(
                incident_id=incident_id,
                version=len(existing) + 1,
                document=last_document,
                quality=last_quality,
                evidence_ids=[item.id for item in selection.evidence],
                model=last_metadata.model,
                prompt_version='v2-symbol-context',
                input_tokens=last_metadata.input_tokens,
                output_tokens=last_metadata.output_tokens,
                duration_ms=last_metadata.duration_ms,
            )
            self.store.save_version(version)
            self._transition(self.store.get_incident(incident_id), IncidentStatus.PENDING_REVIEW)
            return version
        except Exception as exc:
            current = self.store.get_incident(incident_id)
            failed_stage = current.status.value
            if current.status != IncidentStatus.FAILED:
                self.store.update_incident(
                    incident_id,
                    status=IncidentStatus.FAILED,
                    failed_stage=failed_stage,
                    error=str(exc),
                )
            raise

    def retry(self, incident_id: str) -> RetrospectiveVersion:
        incident = self.store.get_incident(incident_id)
        if incident.status != IncidentStatus.FAILED:
            raise PipelineError("Only failed incidents can be retried")
        self.store.update_incident(
            incident_id,
            retry_count=incident.retry_count + 1,
            error="",
            failed_stage="",
        )
        return self.run(incident_id)

    def regenerate(self, incident_id: str) -> RetrospectiveVersion:
        """Regenerate a rejected review from the already persisted evidence set."""
        incident = self.store.get_incident(incident_id)
        if incident.status != IncidentStatus.REJECTED:
            raise PipelineError("Only rejected retrospectives can be regenerated")
        all_evidence = self.store.list_evidence(incident_id)
        previously_selected = [item for item in all_evidence if item.selected]
        if not previously_selected:
            raise PipelineError("No selected evidence is available for regeneration")
        selected = self.selector.select(incident, previously_selected).evidence
        try:
            self._transition(incident, IncidentStatus.GENERATING)
            query = _retrieval_query(incident, selected)
            query_vector = self.embedder.embed_texts([query])[0]
            history = self._retrieve_historical_cases(
                query,
                query_vector,
                repo_id=incident.repo_id,
                incident_type=incident.incident_type,
                limit=self.top_k,
            )
            last_quality: QualityReport | None = None
            last_document: RetrospectiveDocument | None = None
            last_metadata = None
            for attempt in range(self.max_generation_attempts):
                current = self.store.get_incident(incident_id)
                if current.status != IncidentStatus.GENERATING:
                    self._transition(current, IncidentStatus.GENERATING)
                generation_incident = incident
                if last_quality is not None:
                    generation_incident = incident.model_copy(update={
                        "metadata": {**incident.metadata, "generation_validation_errors": last_quality.hard_errors},
                    })
                document, metadata = self._generate(generation_incident, selected, history)
                self._transition(self.store.get_incident(incident_id), IncidentStatus.VALIDATING)
                quality = self.validator.validate(document, selected, history, all_evidence)
                last_quality, last_document, last_metadata = quality, document, metadata
                if quality.hard_passed:
                    break
            if last_quality is None or last_document is None or last_metadata is None:
                raise PipelineError("Generator did not return a retrospective")
            if not last_quality.hard_passed:
                raise PipelineError("; ".join(last_quality.hard_errors))
            versions = self.store.list_versions(incident_id)
            version = RetrospectiveVersion(
                incident_id=incident_id,
                version=len(versions) + 1,
                document=last_document,
                quality=last_quality,
                evidence_ids=[item.id for item in selected],
                model=last_metadata.model,
                prompt_version='v2-symbol-context',
                input_tokens=last_metadata.input_tokens,
                output_tokens=last_metadata.output_tokens,
                duration_ms=last_metadata.duration_ms,
            )
            self.store.save_version(version)
            self._transition(self.store.get_incident(incident_id), IncidentStatus.PENDING_REVIEW)
            return version
        except Exception as exc:
            current = self.store.get_incident(incident_id)
            if current.status != IncidentStatus.FAILED:
                self.store.update_incident(
                    incident_id,
                    status=IncidentStatus.FAILED,
                    failed_stage=current.status.value,
                    error=str(exc),
                )
            raise

    def edit(self, incident_id: str, document: RetrospectiveDocument, actor: str) -> RetrospectiveVersion:
        incident = self.store.get_incident(incident_id)
        if incident.status not in {IncidentStatus.PENDING_REVIEW, IncidentStatus.REJECTED}:
            raise PipelineError("Only pending or rejected retrospectives can be edited")
        evidence = self.store.list_evidence(incident_id)
        quality = self.validator.validate(document, evidence, [])
        if not quality.hard_passed:
            raise PipelineError("; ".join(quality.hard_errors))
        versions = self.store.list_versions(incident_id)
        version = RetrospectiveVersion(
            incident_id=incident_id,
            version=len(versions) + 1,
            kind="human",
            document=document,
            quality=quality,
            evidence_ids=sorted({citation.evidence_id for citation in _document_citations(document)}),
            actor=actor,
        )
        self.store.save_version(version)
        if incident.status == IncidentStatus.REJECTED:
            self._transition(self.store.get_incident(incident_id), IncidentStatus.PENDING_REVIEW, actor=actor, action="submit")
        else:
            self.store.add_review_action(
                ReviewAction(
                    incident_id=incident_id,
                    action="edit",
                    actor=actor,
                    from_status=incident.status,
                    to_status=incident.status,
                )
            )
        return version

    def approve(self, incident_id: str, actor: str, reason: str = "") -> IncidentRecord:
        incident = self.store.get_incident(incident_id)
        return self._transition(incident, IncidentStatus.APPROVED, actor=actor, action="approve", reason=reason)

    def reject(self, incident_id: str, actor: str, reason: str) -> IncidentRecord:
        if not reason.strip():
            raise PipelineError("Rejection reason is required")
        incident = self.store.get_incident(incident_id)
        return self._transition(incident, IncidentStatus.REJECTED, actor=actor, action="reject", reason=reason)

    def publish(self, incident_id: str, actor: str) -> dict[str, Any]:
        incident = self.store.get_incident(incident_id)
        version = self.store.latest_version(incident_id)
        if version is None:
            raise PipelineError("No retrospective version exists")
        existing = self.store.get_publication(version.id)
        if existing:
            if incident.status == IncidentStatus.APPROVED:
                knowledge_id = self._index_published_version(incident, version, existing)
                self.store.update_incident(incident_id, status=IncidentStatus.PUBLISHED)
            else:
                knowledge_id = f"knowledge_{version.id}"
            return {"publication": existing, "knowledge_id": knowledge_id, "idempotent": True}
        if incident.status != IncidentStatus.APPROVED:
            raise PipelineError("Only approved retrospectives can be published")
        self._transition(incident, IncidentStatus.PUBLISHING, actor=actor, action="publish")
        try:
            publication = self.collaboration.publish(incident, version.document, version.id)
            self.store.save_publication(version.id, publication)
            knowledge_id = self._index_published_version(incident, version, publication)
            self._transition(self.store.get_incident(incident_id), IncidentStatus.PUBLISHED, actor=actor, action="publish")
            return {"publication": publication, "knowledge_id": knowledge_id, "idempotent": False}
        except Exception as exc:
            current = self.store.get_incident(incident_id)
            if current.status == IncidentStatus.PUBLISHING:
                self.store.update_incident(incident_id, status=IncidentStatus.APPROVED, failed_stage="publishing", error=str(exc))
            raise

    def _index_published_version(
        self, incident: IncidentRecord, version: RetrospectiveVersion, publication: PublishResult
    ) -> str:
        sections = _knowledge_chunks(version.document)
        # Preserve the full card in storage while bounding each embedding input.
        embeddings = self.embedder.embed_texts([content[:4_000] for content, _ in sections])
        if len(embeddings) != len(sections):
            raise PipelineError("Embedding count does not match knowledge sections")
        return self.store.index_knowledge(
            incident=incident,
            version=version,
            publication=publication,
            chunks=[
                (content, embedding, {**metadata, "format_version": "v3", "embedding_model": str(getattr(self.embedder, "model", ""))})
                for (content, metadata), embedding in zip(sections, embeddings)
            ],
        )

    def search_knowledge(
        self,
        query: str,
        *,
        repo_id: str = "",
        incident_type: str = "",
        limit: int = 5,
    ) -> list[KnowledgeHit]:
        embedding = self.embedder.embed_texts([query])[0]
        return self._retrieve_historical_cases(
            query,
            embedding,
            repo_id=repo_id,
            incident_type=incident_type,
            limit=limit,
        )

    def _retrieve_historical_cases(
        self, query: str, embedding: list[float], *, repo_id: str, incident_type: str, limit: int
    ) -> list[KnowledgeHit]:
        candidates = self.store.search_knowledge(
            query,
            embedding,
            repo_id=repo_id,
            incident_type=incident_type,
            limit=max(limit, self.rerank_pool),
        )
        return _rerank_knowledge_hits(query, candidates, limit)

    def connector_status(self) -> dict[str, Any]:
        storage = self.store.healthcheck()
        feishu = self.collaboration.status()
        openai = self.generator.status()
        github = {"configured": bool(getattr(self.git, "token", True))}
        ready = bool(
            storage.get("ready")
            and github["configured"]
            and feishu.get("configured")
            and feishu.get("wiki_configured")
            and (feishu.get("cache") or {}).get("ready")
            and openai.get("configured")
        )
        return {
            "ready": ready,
            "storage": storage,
            "github": github,
            "code_context": {"enabled": self.code_context is not None},
            "feishu": feishu,
            "openai": openai,
        }

    def _transition(
        self,
        incident: IncidentRecord,
        target: IncidentStatus,
        *,
        actor: str = "system",
        action: str | None = None,
        reason: str = "",
    ) -> IncidentRecord:
        if target == incident.status:
            return incident
        if target not in ALLOWED_STATUS_TRANSITIONS.get(incident.status, set()):
            raise PipelineError(f"Illegal status transition: {incident.status.value} -> {target.value}")
        updated = self.store.update_incident(incident.id, status=target, error="")
        if action in {"submit", "approve", "reject", "publish", "edit"}:
            self.store.add_review_action(
                ReviewAction(
                    incident_id=incident.id,
                    action=action,
                    actor=actor,
                    reason=reason,
                    from_status=incident.status,
                    to_status=target,
                )
            )
        return updated

    def _verify_github_signature(self, raw_body: bytes, signature: str) -> None:
        if not self.github_webhook_secret:
            raise PipelineError("GITHUB_WEBHOOK_SECRET is required")
        if not signature.startswith("sha256="):
            raise PipelineError("Missing or invalid GitHub signature")
        expected = hmac.new(self.github_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
            raise PipelineError("Invalid GitHub signature")


def _github_incident_id(repo_name: str, pr_number: int) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repo_name).strip("-").lower()
    return f"gh_pr_{normalized}_{pr_number}"


def _retrieval_query(incident: IncidentRecord, evidence: list[Evidence]) -> str:
    """Search history with failure and code clues, without embedding whole logs."""
    parts = [incident.title, incident.description[:500], incident.incident_type]
    for item in evidence:
        if item.source_type == EvidenceSource.CI_LOG:
            failure_lines = [
                line.strip()[:220]
                for line in item.content.splitlines()
                if re.search(r"error|failed|exception|traceback|panic", line, re.IGNORECASE)
            ]
            parts.extend(failure_lines[:3])
        elif item.source_type in {EvidenceSource.GITHUB_DIFF, EvidenceSource.REPOSITORY_CODE}:
            parts.append(str(item.metadata.get("file_path") or item.location).split("#", 1)[0])
            parts.extend(re.findall(r"\b(?:def|class|function)\s+([A-Za-z_][A-Za-z0-9_]*)", item.content)[:3])
    return " ".join(dict.fromkeys(part for part in parts if part))[:1_800]


def _rerank_knowledge_hits(query: str, hits: list[KnowledgeHit], limit: int) -> list[KnowledgeHit]:
    if not hits:
        return []
    query_lower = query.lower()
    query_terms = set(_tokens(query))
    max_retrieval_score = max(hit.score for hit in hits) or 1.0
    reranked: list[KnowledgeHit] = []
    for hit in hits:
        modules = hit.metadata.get("related_modules") or []
        keywords = hit.metadata.get("keywords") or []
        module_match = _metadata_match_fraction(modules, query_lower, path_stems=True)
        keyword_match = _metadata_match_fraction(keywords, query_lower)
        title_terms = set(_tokens(hit.title))
        title_match = len(title_terms & query_terms) / max(len(title_terms), 1)
        retrieval_match = hit.score / max_retrieval_score
        score = 0.75 * retrieval_match + 0.12 * module_match + 0.08 * keyword_match + 0.05 * title_match
        reranked.append(hit.model_copy(update={
            "score": round(score, 6),
            "metadata": {
                **hit.metadata,
                "retrieval_score": hit.score,
                "rerank_strategy": "structured_v1",
                "rerank_features": {
                    "module_match": round(module_match, 4),
                    "keyword_match": round(keyword_match, 4),
                    "title_match": round(title_match, 4),
                },
            },
        }))
    reranked.sort(key=lambda item: (-item.score, item.knowledge_id))
    return reranked[:limit]


def _metadata_match_fraction(values: Any, query_lower: str, *, path_stems: bool = False) -> float:
    if not isinstance(values, list):
        return 0.0
    normalized = [str(value).lower().strip() for value in values if str(value).strip()]
    if not normalized:
        return 0.0
    matched = 0
    for value in normalized:
        alternatives = {value}
        if path_stems:
            basename = value.replace("\\", "/").rsplit("/", 1)[-1]
            alternatives.add(basename)
            alternatives.add(basename.rsplit(".", 1)[0])
        if any(term and term in query_lower for term in alternatives):
            matched += 1
    return matched / len(normalized)


def _tokens(value: str) -> list[str]:
    return re.findall(r"[\w.-]+", value.lower(), flags=re.UNICODE)


def _select_log_lines(content: str, query_terms: set[str]) -> str:
    lines = content.splitlines()
    markers = {"error", "warning", "warn", "failed", "failure", "exception", "traceback", "panic", *query_terms}
    selected_indexes: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(marker and marker in lowered for marker in markers):
            selected_indexes.update(range(max(0, index - 2), min(len(lines), index + 3)))
    if not selected_indexes:
        return "\n".join(lines[:120])
    return "\n".join(lines[index] for index in sorted(selected_indexes))


def _select_diff_hunks(content: str, query_terms: set[str]) -> str:
    hunks = re.split(r"(?=^@@)", content, flags=re.MULTILINE)
    relevant = [hunk for hunk in hunks if query_terms & set(_tokens(hunk))]
    return "".join(relevant or hunks[:3])


def _normalize_quote(value: str) -> str:
    return " ".join(value.split())


def _document_citations(document: RetrospectiveDocument) -> list[Citation]:
    return [*document.impact.citations, *document.root_cause.citations, *document.fix_solution.citations]


def _knowledge_chunks(document: RetrospectiveDocument) -> list[tuple[str, dict[str, Any]]]:
    all_ids = sorted({item.evidence_id for item in _document_citations(document)})
    impact_ids = sorted({item.evidence_id for item in document.impact.citations})
    cause_ids = sorted({item.evidence_id for item in document.root_cause.citations})
    fix_ids = sorted({item.evidence_id for item in document.fix_solution.citations})
    title = f"问题：{document.title}"
    modules = "、".join(document.related_modules)
    prevention = "；".join(document.prevention)
    sections = [
        (
            f"{title}\n根因：{document.root_cause.content}\n修复：{document.fix_solution.content}"
            f"\n故障摘要：{document.summary}\n影响：{document.impact.scope}（{document.impact.severity}）"
            f"\n预防：{prevention}\n模块：{modules}\n关键词：{'、'.join(document.keywords)}",
            {"section": "case_card", "source_evidence_ids": all_ids},
        ),
    ]
    section_bodies = [
        ("symptoms", f"故障摘要：{document.summary}\n影响：{document.impact.scope}（{document.impact.severity}）\n模块：{modules}\n关键词：{'、'.join(document.keywords)}", impact_ids),
        ("root_cause", f"根因：{document.root_cause.content}", cause_ids),
        ("fix_solution", f"修复：{document.fix_solution.content}", fix_ids),
        ("prevention", f"预防：{prevention}\n模块：{modules}", []),
    ]
    for section, body, evidence_ids in section_bodies:
        sections.extend(_split_knowledge_section(document.title, section, body, evidence_ids))
    return sections


def _split_knowledge_section(
    title: str, section: str, body: str, evidence_ids: list[str], *, max_chars: int = 1_200, overlap_chars: int = 100
) -> list[tuple[str, dict[str, Any]]]:
    prefix = f"问题：{title[:160]}\n"
    capacity = max_chars - len(prefix)
    chunks: list[tuple[str, dict[str, Any]]] = []
    start = 0
    while start < len(body):
        end = min(len(body), start + capacity)
        if end < len(body):
            boundaries = [body.rfind(marker, start + capacity // 2, end) for marker in ("\n", "。", "；", ";")]
            boundary = max(boundaries)
            if boundary >= 0:
                end = boundary + 1
        chunks.append((
            prefix + body[start:end],
            {"section": section, "segment_index": len(chunks), "source_evidence_ids": evidence_ids},
        ))
        if end == len(body):
            break
        start = max(start + 1, end - overlap_chars)
    return chunks
