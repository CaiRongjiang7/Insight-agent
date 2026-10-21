"""Persistence ports plus PostgreSQL/pgvector and explicit test-memory adapters."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .retrospective_models import (
    Evidence,
    IncidentRecord,
    IncidentStatus,
    KnowledgeHit,
    PublishResult,
    RetrospectiveVersion,
    ReviewAction,
    new_id,
    utc_now,
)


class RetrospectiveStore(Protocol):
    def initialize(self) -> None: ...
    def healthcheck(self) -> dict[str, Any]: ...
    def register_source_event(self, delivery_id: str, event_type: str, payload: dict[str, Any]) -> bool: ...
    def create_incident(self, incident: IncidentRecord) -> IncidentRecord: ...
    def get_incident(self, incident_id: str) -> IncidentRecord: ...
    def list_incidents(self, limit: int = 50) -> list[IncidentRecord]: ...
    def update_incident(self, incident_id: str, **changes: Any) -> IncidentRecord: ...
    def add_evidence(self, evidence: list[Evidence]) -> list[Evidence]: ...
    def index_evidence(self, evidence: list[Evidence], embeddings: list[list[float]]) -> None: ...
    def list_evidence(self, incident_id: str) -> list[Evidence]: ...
    def save_version(self, version: RetrospectiveVersion) -> RetrospectiveVersion: ...
    def list_versions(self, incident_id: str) -> list[RetrospectiveVersion]: ...
    def latest_version(self, incident_id: str) -> RetrospectiveVersion | None: ...
    def add_review_action(self, action: ReviewAction) -> None: ...
    def list_review_actions(self, incident_id: str) -> list[ReviewAction]: ...
    def get_publication(self, version_id: str) -> PublishResult | None: ...
    def save_publication(self, version_id: str, result: PublishResult) -> None: ...
    def index_knowledge(
        self,
        *,
        incident: IncidentRecord,
        version: RetrospectiveVersion,
        publication: PublishResult,
        chunks: list[tuple[str, list[float], dict[str, Any]]],
    ) -> str: ...
    def search_knowledge(
        self,
        query: str,
        embedding: list[float] | None,
        *,
        repo_id: str = "",
        incident_type: str = "",
        limit: int = 5,
    ) -> list[KnowledgeHit]: ...
    def create_job(self, job_id: str, incident_id: str, stage: str, status: str) -> None: ...
    def update_job(self, job_id: str, *, stage: str, status: str, error: str = "") -> None: ...


class InMemoryRetrospectiveStore:
    """Explicitly injected test adapter. Production never selects it implicitly."""

    def __init__(self, *, rrf_k: int = 60, channel_pool: int = 40) -> None:
        self._lock = RLock()
        self.rrf_k = max(1, rrf_k)
        self.channel_pool = max(1, channel_pool)
        self._source_events: set[str] = set()
        self._incidents: dict[str, IncidentRecord] = {}
        self._evidence: dict[str, list[Evidence]] = {}
        self._versions: dict[str, list[RetrospectiveVersion]] = {}
        self._actions: dict[str, list[ReviewAction]] = {}
        self._publications: dict[str, PublishResult] = {}
        self._knowledge: list[dict[str, Any]] = []
        self._evidence_embeddings: dict[str, list[float]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}

    def initialize(self) -> None:
        return None

    def healthcheck(self) -> dict[str, Any]:
        return {"configured": True, "ready": True, "type": "memory", "vector": False}

    def register_source_event(self, delivery_id: str, event_type: str, payload: dict[str, Any]) -> bool:
        del event_type, payload
        with self._lock:
            if delivery_id in self._source_events:
                return False
            self._source_events.add(delivery_id)
            return True

    def create_incident(self, incident: IncidentRecord) -> IncidentRecord:
        with self._lock:
            if incident.id in self._incidents:
                return deepcopy(self._incidents[incident.id])
            self._incidents[incident.id] = deepcopy(incident)
            return deepcopy(incident)

    def get_incident(self, incident_id: str) -> IncidentRecord:
        with self._lock:
            try:
                return deepcopy(self._incidents[incident_id])
            except KeyError as exc:
                raise KeyError(f"Incident not found: {incident_id}") from exc

    def list_incidents(self, limit: int = 50) -> list[IncidentRecord]:
        with self._lock:
            values = sorted(self._incidents.values(), key=lambda item: item.created_at, reverse=True)
            return deepcopy(values[:limit])

    def update_incident(self, incident_id: str, **changes: Any) -> IncidentRecord:
        with self._lock:
            incident = self.get_incident(incident_id)
            data = incident.model_dump()
            data.update(changes)
            data["updated_at"] = utc_now()
            updated = IncidentRecord.model_validate(data)
            self._incidents[incident_id] = updated
            return deepcopy(updated)

    def add_evidence(self, evidence: list[Evidence]) -> list[Evidence]:
        with self._lock:
            for item in evidence:
                current = self._evidence.setdefault(item.incident_id, [])
                existing_index = next((index for index, existing in enumerate(current) if existing.id == item.id), None)
                if existing_index is None:
                    current.append(deepcopy(item))
                else:
                    existing = current[existing_index]
                    updated = existing.model_copy(
                        update={"selected": item.selected, "selection_reason": item.selection_reason}
                    )
                    current[existing_index] = updated
            return deepcopy(evidence)

    def list_evidence(self, incident_id: str) -> list[Evidence]:
        with self._lock:
            return deepcopy(self._evidence.get(incident_id, []))

    def index_evidence(self, evidence: list[Evidence], embeddings: list[list[float]]) -> None:
        if len(evidence) != len(embeddings):
            raise ValueError("Evidence and embedding counts must match")
        with self._lock:
            for item, embedding in zip(evidence, embeddings):
                self._evidence_embeddings[item.id] = list(embedding)

    def create_job(self, job_id: str, incident_id: str, stage: str, status: str) -> None:
        with self._lock:
            self._jobs.setdefault(job_id, {"incident_id": incident_id, "stage": stage, "status": status, "error": ""})

    def update_job(self, job_id: str, *, stage: str, status: str, error: str = "") -> None:
        with self._lock:
            job = self._jobs.setdefault(job_id, {"incident_id": ""})
            job.update({"stage": stage, "status": status, "error": error})

    def save_version(self, version: RetrospectiveVersion) -> RetrospectiveVersion:
        with self._lock:
            versions = self._versions.setdefault(version.incident_id, [])
            versions.append(deepcopy(version))
            self.update_incident(version.incident_id, current_version=version.version)
            return deepcopy(version)

    def list_versions(self, incident_id: str) -> list[RetrospectiveVersion]:
        with self._lock:
            return deepcopy(self._versions.get(incident_id, []))

    def latest_version(self, incident_id: str) -> RetrospectiveVersion | None:
        versions = self.list_versions(incident_id)
        return versions[-1] if versions else None

    def add_review_action(self, action: ReviewAction) -> None:
        with self._lock:
            self._actions.setdefault(action.incident_id, []).append(deepcopy(action))

    def list_review_actions(self, incident_id: str) -> list[ReviewAction]:
        with self._lock:
            return deepcopy(self._actions.get(incident_id, []))

    def get_publication(self, version_id: str) -> PublishResult | None:
        with self._lock:
            result = self._publications.get(version_id)
            return deepcopy(result) if result else None

    def save_publication(self, version_id: str, result: PublishResult) -> None:
        with self._lock:
            self._publications.setdefault(version_id, deepcopy(result))

    def index_knowledge(
        self,
        *,
        incident: IncidentRecord,
        version: RetrospectiveVersion,
        publication: PublishResult,
        chunks: list[tuple[str, list[float], dict[str, Any]]],
    ) -> str:
        with self._lock:
            knowledge_id = f"knowledge_{version.id}"
            self._knowledge = [item for item in self._knowledge if item["knowledge_id"] != knowledge_id]
            for index, (content, embedding, metadata) in enumerate(chunks):
                self._knowledge.append(
                    {
                        "knowledge_id": knowledge_id,
                        "chunk_id": f"{knowledge_id}_{index}",
                        "incident_id": incident.id,
                        "repo_id": incident.repo_id,
                        "incident_type": incident.incident_type,
                        "title": version.document.title,
                        "content": content,
                        "embedding": list(embedding),
                        "source_url": publication.url,
                        "metadata": {
                            "version_id": version.id,
                            "status": "published",
                            "chunk_index": index,
                            "keywords": version.document.keywords,
                            "related_modules": version.document.related_modules,
                            "severity": version.document.impact.severity,
                            "commit_sha": incident.commit_sha,
                            **metadata,
                        },
                    }
                )
            return knowledge_id

    def search_knowledge(
        self,
        query: str,
        embedding: list[float] | None,
        *,
        repo_id: str = "",
        incident_type: str = "",
        limit: int = 5,
    ) -> list[KnowledgeHit]:
        query_terms = {term for term in _tokenize(query) if len(term) > 1}
        pool_size = max(self.channel_pool, limit * 2)
        scored_chunks: list[tuple[dict[str, Any], float, float]] = []
        with self._lock:
            case_cards = {
                item["knowledge_id"]: item
                for item in self._knowledge if item["metadata"].get("section") == "case_card"
            }
            for item in self._knowledge:
                if repo_id and item["repo_id"] != repo_id:
                    continue
                if incident_type and item["incident_type"] != incident_type:
                    continue
                content_terms = set(_tokenize(f"{item['title']} {item['content']}"))
                lexical = len(query_terms & content_terms) / max(len(query_terms), 1)
                vector = max(_cosine(embedding or [], item["embedding"]), 0.0)
                if lexical > 0 or vector > 0:
                    scored_chunks.append((item, lexical, vector))

            def best_cases(channel: int) -> list[tuple[dict[str, Any], float, float]]:
                ranked = sorted(
                    (row for row in scored_chunks if row[channel] > 0),
                    key=lambda row: (-row[channel], row[0]["chunk_id"]),
                )
                distinct: dict[str, tuple[dict[str, Any], float, float]] = {}
                for row in ranked:
                    distinct.setdefault(row[0]["knowledge_id"], row)
                return list(distinct.values())[:pool_size]

            fused: dict[str, float] = {}
            matched_chunks: dict[str, dict[str, tuple[tuple[dict[str, Any], float, float], float]]] = {}
            for channel_rows in (best_cases(1), best_cases(2) if embedding else []):
                for position, row in enumerate(channel_rows, 1):
                    item = row[0]
                    case_id = item["knowledge_id"]
                    contribution = 1 / (self.rrf_k + position)
                    fused[case_id] = fused.get(case_id, 0.0) + contribution
                    chunk_id = item["chunk_id"]
                    case_chunks = matched_chunks.setdefault(case_id, {})
                    previous = case_chunks.get(chunk_id)
                    case_chunks[chunk_id] = (row, contribution + (previous[1] if previous else 0.0))

            hits: list[KnowledgeHit] = []
            for case_id, score in fused.items():
                item, lexical, vector = max(
                    matched_chunks[case_id].values(),
                    key=lambda candidate: (candidate[1], candidate[0][2], candidate[0][1], candidate[0][0]["chunk_id"]),
                )[0]
                hit = KnowledgeHit(
                    knowledge_id=case_id,
                    incident_id=item["incident_id"],
                    title=item["title"],
                    content=case_cards.get(item["knowledge_id"], item)["content"],
                    score=round(score, 6),
                    lexical_score=round(lexical, 6),
                    vector_score=round(vector, 6),
                    source_url=item["source_url"],
                    metadata={
                        **item["metadata"],
                        "matched_section": item["metadata"].get("section", ""),
                        "case_source_evidence_ids": case_cards.get(item["knowledge_id"], item)["metadata"].get("source_evidence_ids", []),
                        "retrieval_strategy": "rrf",
                        "rrf_k": self.rrf_k,
                        "candidate_pool_size": pool_size,
                    },
                )
                hits.append(hit)
        hits.sort(key=lambda item: (-item.score, item.knowledge_id))
        return hits[:limit]


class PostgresRetrospectiveStore:
    """PostgreSQL is the production source of truth; pgvector serves embeddings."""

    def __init__(
        self, database_url: str, schema_path: str | Path | None = None, *, rrf_k: int = 60, channel_pool: int = 40
    ) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required; production storage cannot fall back to local files")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - exercised in container
            raise RuntimeError("PostgreSQL pipeline requires psycopg[binary]") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._Jsonb = Jsonb
        self.database_url = database_url
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).with_name("retrospective_schema.sql")
        self.rrf_k = max(1, rrf_k)
        self.channel_pool = max(1, channel_pool)

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def initialize(self) -> None:
        sql = self.schema_path.read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(sql)

    def healthcheck(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS vector,
                       to_regclass('public.incidents') IS NOT NULL AS schema_ready
                """
            ).fetchone()
        ready = bool(row and row["vector"] and row["schema_ready"])
        return {"configured": True, "ready": ready, "type": "postgresql", "vector": bool(row and row["vector"])}

    def register_source_event(self, delivery_id: str, event_type: str, payload: dict[str, Any]) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO source_events (delivery_id, event_type, payload)
                VALUES (%s, %s, %s) ON CONFLICT (delivery_id) DO NOTHING
                RETURNING delivery_id
                """,
                (delivery_id, event_type, self._Jsonb(payload)),
            ).fetchone()
        return row is not None

    def create_incident(self, incident: IncidentRecord) -> IncidentRecord:
        data = incident.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incidents (
                    id, trace_id, title, description, repo_id, repo_name, incident_type,
                    source_type, pr_number, commit_sha, source_url, logs, status,
                    current_version, retry_count, failed_stage, error, metadata,
                    feishu_chat_ids, feishu_doc_tokens, created_at, updated_at
                ) VALUES (
                    %(id)s, %(trace_id)s, %(title)s, %(description)s, %(repo_id)s,
                    %(repo_name)s, %(incident_type)s, %(source_type)s, %(pr_number)s,
                    %(commit_sha)s, %(source_url)s, %(logs)s, %(status)s,
                    %(current_version)s, %(retry_count)s, %(failed_stage)s, %(error)s,
                    %(metadata)s, %(feishu_chat_ids)s, %(feishu_doc_tokens)s,
                    %(created_at)s, %(updated_at)s
                ) ON CONFLICT (id) DO NOTHING
                """,
                {
                    **data,
                    "metadata": self._Jsonb(data["metadata"]),
                    "feishu_chat_ids": self._Jsonb(data["feishu_chat_ids"]),
                    "feishu_doc_tokens": self._Jsonb(data["feishu_doc_tokens"]),
                },
            )
        return self.get_incident(incident.id)

    def get_incident(self, incident_id: str) -> IncidentRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,)).fetchone()
        if row is None:
            raise KeyError(f"Incident not found: {incident_id}")
        return _incident_from_row(row)

    def list_incidents(self, limit: int = 50) -> list[IncidentRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [_incident_from_row(row) for row in rows]

    def update_incident(self, incident_id: str, **changes: Any) -> IncidentRecord:
        current = self.get_incident(incident_id).model_dump(mode="json")
        current.update(changes)
        current["updated_at"] = utc_now()
        incident = IncidentRecord.model_validate(current)
        data = incident.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE incidents SET status=%(status)s, current_version=%(current_version)s,
                    retry_count=%(retry_count)s, failed_stage=%(failed_stage)s, error=%(error)s,
                    metadata=%(metadata)s, updated_at=%(updated_at)s
                WHERE id=%(id)s
                """,
                {**data, "metadata": self._Jsonb(data["metadata"])},
            )
        return self.get_incident(incident_id)

    def add_evidence(self, evidence: list[Evidence]) -> list[Evidence]:
        with self._connect() as connection:
            for item in evidence:
                data = item.model_dump(mode="json")
                connection.execute(
                    """
                    INSERT INTO evidence_documents (
                        id, incident_id, source_type, content, location, source_url,
                        repo_id, commit_sha, occurred_at, metadata, selected, selection_reason
                    ) VALUES (
                        %(id)s, %(incident_id)s, %(source_type)s, %(content)s, %(location)s,
                        %(source_url)s, %(repo_id)s, %(commit_sha)s, %(occurred_at)s,
                        %(metadata)s, %(selected)s, %(selection_reason)s
                    ) ON CONFLICT (id) DO UPDATE SET selected=EXCLUDED.selected,
                        selection_reason=EXCLUDED.selection_reason
                    """,
                    {**data, "metadata": self._Jsonb(data["metadata"])},
                )
        return evidence

    def list_evidence(self, incident_id: str) -> list[Evidence]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_documents WHERE incident_id=%s ORDER BY occurred_at, id",
                (incident_id,),
            ).fetchall()
        return [Evidence.model_validate(_normalize_row(row)) for row in rows]

    def index_evidence(self, evidence: list[Evidence], embeddings: list[list[float]]) -> None:
        if len(evidence) != len(embeddings):
            raise ValueError("Evidence and embedding counts must match")
        with self._connect() as connection:
            for item, embedding in zip(evidence, embeddings):
                connection.execute(
                    """
                    INSERT INTO evidence_chunks (
                        id, evidence_id, incident_id, source_type, content, embedding, metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s::vector,%s)
                    ON CONFLICT (id) DO UPDATE SET content=EXCLUDED.content,
                        embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata
                    """,
                    (
                        f"{item.id}_0",
                        item.id,
                        item.incident_id,
                        item.source_type.value,
                        item.content,
                        _vector_literal(embedding),
                        self._Jsonb({"location": item.location, "source_url": item.source_url}),
                    ),
                )

    def create_job(self, job_id: str, incident_id: str, stage: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (id, incident_id, stage, status, trace_id)
                SELECT %s, id, %s, %s, trace_id FROM incidents WHERE id=%s
                ON CONFLICT (id) DO NOTHING
                """,
                (job_id, stage, status, incident_id),
            )

    def update_job(self, job_id: str, *, stage: str, status: str, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET stage=%s, status=%s, error=%s, updated_at=now()
                WHERE id=%s
                """,
                (stage, status, error, job_id),
            )

    def save_version(self, version: RetrospectiveVersion) -> RetrospectiveVersion:
        data = version.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO retrospective_versions (
                    id, incident_id, version, kind, document, quality, evidence_ids,
                    model, prompt_version, input_tokens, output_tokens, duration_ms,
                    actor, created_at
                ) VALUES (
                    %(id)s, %(incident_id)s, %(version)s, %(kind)s, %(document)s,
                    %(quality)s, %(evidence_ids)s, %(model)s, %(prompt_version)s,
                    %(input_tokens)s, %(output_tokens)s, %(duration_ms)s, %(actor)s,
                    %(created_at)s
                )
                """,
                {
                    **data,
                    "document": self._Jsonb(data["document"]),
                    "quality": self._Jsonb(data["quality"]),
                    "evidence_ids": self._Jsonb(data["evidence_ids"]),
                },
            )
            connection.execute(
                "UPDATE incidents SET current_version=%s, updated_at=%s WHERE id=%s",
                (version.version, utc_now(), version.incident_id),
            )
        return version

    def list_versions(self, incident_id: str) -> list[RetrospectiveVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM retrospective_versions WHERE incident_id=%s ORDER BY version",
                (incident_id,),
            ).fetchall()
        return [_version_from_row(row) for row in rows]

    def latest_version(self, incident_id: str) -> RetrospectiveVersion | None:
        versions = self.list_versions(incident_id)
        return versions[-1] if versions else None

    def add_review_action(self, action: ReviewAction) -> None:
        data = action.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO review_actions (
                    id, incident_id, action, actor, reason, from_status, to_status, created_at
                ) VALUES (
                    %(id)s, %(incident_id)s, %(action)s, %(actor)s, %(reason)s,
                    %(from_status)s, %(to_status)s, %(created_at)s
                )
                """,
                data,
            )

    def list_review_actions(self, incident_id: str) -> list[ReviewAction]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_actions WHERE incident_id=%s ORDER BY created_at", (incident_id,)
            ).fetchall()
        return [ReviewAction.model_validate(_normalize_row(row)) for row in rows]

    def get_publication(self, version_id: str) -> PublishResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT remote_id, url, created FROM publications WHERE retrospective_version_id=%s",
                (version_id,),
            ).fetchone()
        return PublishResult.model_validate(_normalize_row(row)) if row else None

    def save_publication(self, version_id: str, result: PublishResult) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO publications (retrospective_version_id, remote_id, url, created)
                VALUES (%s, %s, %s, %s) ON CONFLICT (retrospective_version_id) DO NOTHING
                """,
                (version_id, result.remote_id, result.url, result.created),
            )

    def index_knowledge(
        self,
        *,
        incident: IncidentRecord,
        version: RetrospectiveVersion,
        publication: PublishResult,
        chunks: list[tuple[str, list[float], dict[str, Any]]],
    ) -> str:
        knowledge_id = f"knowledge_{version.id}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_entries (
                    id, incident_id, retrospective_version_id, repo_id, incident_type,
                    title, source_url, status, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'published',%s)
                ON CONFLICT (retrospective_version_id) DO UPDATE SET
                    title=EXCLUDED.title, source_url=EXCLUDED.source_url,
                    status=EXCLUDED.status, metadata=EXCLUDED.metadata
                """,
                (
                    knowledge_id,
                    incident.id,
                    version.id,
                    incident.repo_id,
                    incident.incident_type,
                    version.document.title,
                    publication.url,
                    self._Jsonb({
                        "keywords": version.document.keywords,
                        "related_modules": version.document.related_modules,
                        "severity": version.document.impact.severity,
                        "commit_sha": incident.commit_sha,
                        "version_id": version.id,
                    }),
                ),
            )
            connection.execute("DELETE FROM knowledge_chunks WHERE knowledge_id=%s", (knowledge_id,))
            for index, (content, embedding, metadata) in enumerate(chunks):
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks (id, knowledge_id, content, embedding, metadata)
                    VALUES (%s,%s,%s,%s::vector,%s)
                    """,
                    (
                        f"{knowledge_id}_{index}",
                        knowledge_id,
                        content,
                        _vector_literal(embedding),
                        self._Jsonb({"chunk_index": index, **metadata}),
                    ),
                )
        return knowledge_id

    def search_knowledge(
        self,
        query: str,
        embedding: list[float] | None,
        *,
        repo_id: str = "",
        incident_type: str = "",
        limit: int = 5,
    ) -> list[KnowledgeHit]:
        filters = "AND (%(repo_id)s = '' OR k.repo_id = %(repo_id)s) AND (%(incident_type)s = '' OR k.incident_type = %(incident_type)s)"
        params: dict[str, Any] = {
            "fts_query": " OR ".join(dict.fromkeys(_tokenize(query)[:40])),
            "repo_id": repo_id,
            "incident_type": incident_type,
            "limit": limit,
            "pool_size": max(self.channel_pool, limit * 2),
            "rrf_k": self.rrf_k,
            "has_embedding": bool(embedding),
        }
        if embedding:
            params["embedding"] = _vector_literal(embedding)
            vector_sql = "GREATEST(0, 1 - (c.embedding <=> %(embedding)s::vector))"
        else:
            vector_sql = "0::double precision"
        sql = f"""
            WITH scored AS (
                SELECT k.id AS knowledge_id, k.incident_id, k.title,
                    card.content, c.id AS matched_chunk_id,
                    ts_rank_cd(to_tsvector('simple', k.title || ' ' || c.content), websearch_to_tsquery('simple', %(fts_query)s)) AS lexical_score,
                    {vector_sql} AS vector_score,
                    k.source_url, k.metadata AS entry_metadata,
                    c.metadata AS chunk_metadata, card.metadata AS case_card_metadata
                FROM knowledge_entries k
                JOIN knowledge_chunks c ON c.knowledge_id = k.id
                JOIN knowledge_chunks card ON card.id = k.id || '_0'
                WHERE k.status = 'published' {filters}
            ), lexical_best AS (
                SELECT scored.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY knowledge_id ORDER BY lexical_score DESC, matched_chunk_id
                    ) AS section_rank
                FROM scored WHERE lexical_score > 0
            ), lexical_ranked AS (
                SELECT knowledge_id, matched_chunk_id,
                    ROW_NUMBER() OVER (ORDER BY lexical_score DESC, matched_chunk_id) AS position
                FROM lexical_best WHERE section_rank = 1
                ORDER BY lexical_score DESC, matched_chunk_id
                LIMIT %(pool_size)s
            ), vector_best AS (
                SELECT scored.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY knowledge_id ORDER BY vector_score DESC, matched_chunk_id
                    ) AS section_rank
                FROM scored WHERE %(has_embedding)s AND vector_score > 0
            ), vector_ranked AS (
                SELECT knowledge_id, matched_chunk_id,
                    ROW_NUMBER() OVER (ORDER BY vector_score DESC, matched_chunk_id) AS position
                FROM vector_best WHERE section_rank = 1
                ORDER BY vector_score DESC, matched_chunk_id
                LIMIT %(pool_size)s
            ), fused_cases AS (
                SELECT knowledge_id, SUM(channel_score) AS score
                FROM (
                    SELECT knowledge_id, 1.0 / (%(rrf_k)s + position) AS channel_score FROM lexical_ranked
                    UNION ALL
                    SELECT knowledge_id, 1.0 / (%(rrf_k)s + position) AS channel_score FROM vector_ranked
                ) channels
                GROUP BY knowledge_id
            ), ranked AS (
                SELECT scored.*, fused_cases.score,
                    ROW_NUMBER() OVER (
                        PARTITION BY scored.knowledge_id
                        ORDER BY (
                            COALESCE(1.0 / (%(rrf_k)s + lexical_ranked.position), 0) +
                            COALESCE(1.0 / (%(rrf_k)s + vector_ranked.position), 0)
                        ) DESC, scored.vector_score DESC, scored.lexical_score DESC, scored.matched_chunk_id
                    ) AS case_rank
                FROM scored
                JOIN fused_cases ON fused_cases.knowledge_id = scored.knowledge_id
                LEFT JOIN lexical_ranked ON lexical_ranked.matched_chunk_id = scored.matched_chunk_id
                LEFT JOIN vector_ranked ON vector_ranked.matched_chunk_id = scored.matched_chunk_id
            )
            SELECT * FROM ranked WHERE case_rank = 1
            ORDER BY score DESC, knowledge_id
            LIMIT %(limit)s
        """
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            KnowledgeHit(
                knowledge_id=row["knowledge_id"],
                incident_id=row["incident_id"],
                title=row["title"],
                content=row["content"],
                score=round(float(row["score"]), 6),
                lexical_score=float(row["lexical_score"]),
                vector_score=float(row["vector_score"]),
                source_url=row["source_url"],
                metadata={
                    **dict(row["entry_metadata"] or {}),
                    **dict(row["chunk_metadata"] or {}),
                    "matched_section": dict(row["chunk_metadata"] or {}).get("section", ""),
                    "case_source_evidence_ids": dict(row["case_card_metadata"] or {}).get("source_evidence_ids", []),
                    "retrieval_strategy": "rrf",
                    "rrf_k": self.rrf_k,
                    "candidate_pool_size": params["pool_size"],
                },
            )
            for row in rows
        ]


def _tokenize(value: str) -> list[str]:
    import re

    return re.findall(r"[\w.-]+", value.lower(), flags=re.UNICODE)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


def _incident_from_row(row: dict[str, Any]) -> IncidentRecord:
    return IncidentRecord.model_validate(_normalize_row(row))


def _version_from_row(row: dict[str, Any]) -> RetrospectiveVersion:
    return RetrospectiveVersion.model_validate(_normalize_row(row))


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    from datetime import date, datetime

    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (datetime, date)):
            result[key] = value.isoformat()
    return result
