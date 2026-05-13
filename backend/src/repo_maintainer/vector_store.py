from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import RetrievalConfig
from .embeddings import EmbeddingClient, cosine_similarity


class VectorStoreError(RuntimeError):
    pass


class BaseVectorStore:
    backend_name = "base"

    def ensure_documents(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        embedding_client: EmbeddingClient,
        *,
        signature_length: int,
    ) -> dict[str, list[float]]:
        raise NotImplementedError

    def ann_search(
        self,
        collection: str,
        item_ids: list[str],
        query_vector: list[float],
        *,
        signature_length: int,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        raise NotImplementedError


class SQLiteVectorStore(BaseVectorStore):
    backend_name = "sqlite_ann"

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def ensure_documents(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        embedding_client: EmbeddingClient,
        *,
        signature_length: int,
    ) -> dict[str, list[float]]:
        existing: dict[str, tuple[str, list[float]]] = {}
        item_ids = [str(document["item_id"]) for document in documents]
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            with sqlite3.connect(self.database_path) as connection:
                rows = connection.execute(
                    f"""
                    SELECT item_id, text_hash, vector
                    FROM vectors
                    WHERE collection = ? AND item_id IN ({placeholders})
                    """,
                    [collection, *item_ids],
                ).fetchall()
            for item_id, text_hash, vector_blob in rows:
                existing[str(item_id)] = (str(text_hash), json.loads(vector_blob))

        pending: list[dict[str, Any]] = []
        vectors: dict[str, list[float]] = {}
        for document in documents:
            item_id = str(document["item_id"])
            text_hash = self._text_hash(str(document.get("text", "")))
            row = existing.get(item_id)
            if row is not None and row[0] == text_hash:
                vectors[item_id] = [float(value) for value in row[1]]
                continue
            pending.append({**document, "text_hash": text_hash})

        if pending:
            generated = embedding_client.embed_texts([str(document.get("text", "")) for document in pending])
            with sqlite3.connect(self.database_path) as connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO vectors (
                        collection, item_id, text_hash, ann_signature, vector, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            collection,
                            str(document["item_id"]),
                            str(document["text_hash"]),
                            self.signature(generated[index], signature_length=signature_length),
                            json.dumps(generated[index], ensure_ascii=False),
                            json.dumps(document.get("payload", {}), ensure_ascii=False),
                        )
                        for index, document in enumerate(pending)
                    ],
                )
                connection.commit()
            for document, vector in zip(pending, generated):
                vectors[str(document["item_id"])] = vector
        return vectors

    def ann_search(
        self,
        collection: str,
        item_ids: list[str],
        query_vector: list[float],
        *,
        signature_length: int,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        query_signature = self.signature(query_vector, signature_length=signature_length)
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT item_id, ann_signature, vector
                FROM vectors
                WHERE collection = ? AND item_id IN ({placeholders})
                """,
                [collection, *item_ids],
            ).fetchall()
        ranked: list[tuple[tuple[int, float], str]] = []
        for item_id, ann_signature, vector_blob in rows:
            vector = [float(value) for value in json.loads(vector_blob)]
            distance = self._hamming_distance(query_signature, str(ann_signature))
            semantic = cosine_similarity(query_vector, vector)
            ranked.append(((distance, -semantic), str(item_id)))
        ranked.sort(key=lambda item: item[0])
        max_candidates = max(limit * max(probe_count, 1), limit)
        return [item_id for _, item_id in ranked[:max_candidates]]

    def _init_db(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    collection TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    ann_signature TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (collection, item_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vectors_collection_signature
                ON vectors(collection, ann_signature)
                """
            )
            connection.commit()

    def signature(self, vector: list[float], *, signature_length: int) -> str:
        if not vector:
            return "0" * signature_length
        length = max(signature_length, 4)
        chunk_size = max(len(vector) // length, 1)
        bits: list[str] = []
        for offset in range(length):
            start = offset * chunk_size
            end = min(len(vector), start + chunk_size)
            window = vector[start:end] or [0.0]
            bits.append("1" if sum(window) >= 0 else "0")
        return "".join(bits)

    def _text_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _hamming_distance(self, left: str, right: str) -> int:
        size = min(len(left), len(right))
        distance = sum(1 for index in range(size) if left[index] != right[index])
        return distance + abs(len(left) - len(right))


class _PersistentJsonVectorStore(BaseVectorStore):
    backend_name = "json_vectors"

    def __init__(self, storage_path: str | Path) -> None:
        base = Path(storage_path)
        self.storage_dir = (base.with_suffix("") if base.suffix else base) / self.backend_name
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def ensure_documents(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        embedding_client: EmbeddingClient,
        *,
        signature_length: int,
    ) -> dict[str, list[float]]:
        entries = self._load_entries(collection)
        pending: list[dict[str, Any]] = []
        for document in documents:
            item_id = str(document["item_id"])
            text_hash = self._text_hash(str(document.get("text", "")))
            current = entries.get(item_id)
            if current is not None and current.get("text_hash") == text_hash:
                continue
            pending.append({**document, "text_hash": text_hash})

        if pending:
            vectors = embedding_client.embed_texts([str(document.get("text", "")) for document in pending])
            for document, vector in zip(pending, vectors):
                entries[str(document["item_id"])] = {
                    "item_id": str(document["item_id"]),
                    "text_hash": str(document["text_hash"]),
                    "payload": dict(document.get("payload", {})),
                    "vector": [float(value) for value in vector],
                }
            preferred_dimension = len(vectors[0]) if vectors else 0
            entries = self._prune_entries_by_dimension(entries, preferred_dimension=preferred_dimension)
            self._save_entries(collection, entries)
            self._rebuild_index(collection, entries)

        return {
            str(document["item_id"]): [float(value) for value in entries[str(document["item_id"])]["vector"]]
            for document in documents
            if str(document["item_id"]) in entries
        }

    def ann_search(
        self,
        collection: str,
        item_ids: list[str],
        query_vector: list[float],
        *,
        signature_length: int,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        entries = self._load_entries(collection)
        allowed = {str(item_id) for item_id in item_ids}
        if not allowed:
            return []
        return self._ann_search_entries(collection, entries, allowed, query_vector, probe_count=probe_count, limit=limit)

    def _metadata_path(self, collection: str) -> Path:
        return self.storage_dir / f"{self._slug(collection)}.json"

    def _load_entries(self, collection: str) -> dict[str, dict[str, Any]]:
        metadata_path = self._metadata_path(collection)
        if not metadata_path.exists():
            return {}
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        entries = payload.get("entries", {})
        if not isinstance(entries, dict):
            return {}
        return {
            str(item_id): {
                "item_id": str(value.get("item_id", item_id)),
                "text_hash": str(value.get("text_hash", "")),
                "payload": dict(value.get("payload", {})) if isinstance(value.get("payload", {}), dict) else {},
                "vector": [float(item) for item in value.get("vector", [])],
            }
            for item_id, value in entries.items()
            if isinstance(value, dict)
        }

    def _save_entries(self, collection: str, entries: dict[str, dict[str, Any]]) -> None:
        metadata_path = self._metadata_path(collection)
        metadata_path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _prune_entries_by_dimension(
        self,
        entries: dict[str, dict[str, Any]],
        *,
        preferred_dimension: int = 0,
    ) -> dict[str, dict[str, Any]]:
        dimensions: dict[int, int] = {}
        for entry in entries.values():
            vector = entry.get("vector", [])
            if isinstance(vector, list) and vector:
                dimensions[len(vector)] = dimensions.get(len(vector), 0) + 1
        if not dimensions:
            return {}
        target_dimension = preferred_dimension if preferred_dimension in dimensions else max(dimensions.items(), key=lambda item: item[1])[0]
        return {
            item_id: entry
            for item_id, entry in entries.items()
            if isinstance(entry.get("vector", []), list)
            and len(entry.get("vector", [])) == target_dimension
        }

    def _rebuild_index(self, collection: str, entries: dict[str, dict[str, Any]]) -> None:
        raise NotImplementedError

    def _ann_search_entries(
        self,
        collection: str,
        entries: dict[str, dict[str, Any]],
        allowed: set[str],
        query_vector: list[float],
        *,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        raise NotImplementedError

    def _slug(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower()).strip("-")
        return cleaned or "collection"

    def _text_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FaissVectorStore(_PersistentJsonVectorStore):
    backend_name = "faiss"

    def __init__(self, storage_path: str | Path, *, use_hnsw: bool = False, hnsw_m: int = 16) -> None:
        self.use_hnsw = use_hnsw
        self.hnsw_m = max(hnsw_m, 4)
        try:
            import faiss  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VectorStoreError("FAISS backend requires `faiss-cpu`.") from exc
        super().__init__(storage_path)

    @property
    def backend_name(self) -> str:  # type: ignore[override]
        return "faiss_hnsw" if self.use_hnsw else "faiss_flat"

    def _index_path(self, collection: str) -> Path:
        return self.storage_dir / f"{self._slug(collection)}.faiss"

    def _rebuild_index(self, collection: str, entries: dict[str, dict[str, Any]]) -> None:
        import faiss
        import numpy as np

        index_path = self._index_path(collection)
        vectors = [entry.get("vector", []) for entry in entries.values()]
        if not vectors:
            if index_path.exists():
                index_path.unlink()
            return
        matrix = np.array(vectors, dtype="float32")
        faiss.normalize_L2(matrix)
        dimension = matrix.shape[1]
        if self.use_hnsw:
            index = faiss.IndexHNSWFlat(dimension, self.hnsw_m, faiss.METRIC_INNER_PRODUCT)
        else:
            index = faiss.IndexFlatIP(dimension)
        index.add(matrix)
        faiss.write_index(index, str(index_path))

    def _ann_search_entries(
        self,
        collection: str,
        entries: dict[str, dict[str, Any]],
        allowed: set[str],
        query_vector: list[float],
        *,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        import faiss
        import numpy as np

        index_path = self._index_path(collection)
        ordered_ids = list(entries.keys())
        if not ordered_ids or not index_path.exists():
            return []
        query = np.array([query_vector], dtype="float32")
        faiss.normalize_L2(query)
        index = faiss.read_index(str(index_path))
        k = min(max(limit * max(probe_count, 1), limit), len(ordered_ids))
        _, indices = index.search(query, k)
        ranked: list[str] = []
        for item_index in indices[0]:
            if item_index < 0 or item_index >= len(ordered_ids):
                continue
            item_id = ordered_ids[int(item_index)]
            if item_id in allowed and item_id not in ranked:
                ranked.append(item_id)
        if len(ranked) >= limit:
            return ranked
        return ranked + self._fallback_rank(entries, allowed, query_vector, exclude=set(ranked), limit=limit - len(ranked))

    def _fallback_rank(
        self,
        entries: dict[str, dict[str, Any]],
        allowed: set[str],
        query_vector: list[float],
        *,
        exclude: set[str],
        limit: int,
    ) -> list[str]:
        ranked: list[tuple[float, str]] = []
        for item_id, entry in entries.items():
            if item_id not in allowed or item_id in exclude:
                continue
            ranked.append((cosine_similarity(query_vector, [float(value) for value in entry.get("vector", [])]), item_id))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item_id for _, item_id in ranked[:limit]]


class HnswlibVectorStore(_PersistentJsonVectorStore):
    backend_name = "hnswlib"

    def __init__(self, storage_path: str | Path, *, space: str = "cosine", m: int = 16, ef_construction: int = 200, ef_search: int = 64) -> None:
        self.space = space
        self.m = max(m, 4)
        self.ef_construction = max(ef_construction, 32)
        self.ef_search = max(ef_search, 16)
        try:
            import hnswlib  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VectorStoreError("HNSW backend requires `hnswlib`.") from exc
        super().__init__(storage_path)

    def _index_path(self, collection: str) -> Path:
        return self.storage_dir / f"{self._slug(collection)}.bin"

    def _rebuild_index(self, collection: str, entries: dict[str, dict[str, Any]]) -> None:
        import hnswlib
        import numpy as np

        index_path = self._index_path(collection)
        ordered_ids = list(entries.keys())
        if not ordered_ids:
            if index_path.exists():
                index_path.unlink()
            return
        matrix = np.array([entries[item_id]["vector"] for item_id in ordered_ids], dtype="float32")
        dimension = matrix.shape[1]
        index = hnswlib.Index(space=self.space, dim=dimension)
        index.init_index(max_elements=len(ordered_ids), ef_construction=self.ef_construction, M=self.m)
        index.add_items(matrix, list(range(len(ordered_ids))))
        index.set_ef(self.ef_search)
        index.save_index(str(index_path))

    def _ann_search_entries(
        self,
        collection: str,
        entries: dict[str, dict[str, Any]],
        allowed: set[str],
        query_vector: list[float],
        *,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        import hnswlib
        import numpy as np

        index_path = self._index_path(collection)
        ordered_ids = list(entries.keys())
        if not ordered_ids or not index_path.exists():
            return []
        dimension = len(entries[ordered_ids[0]].get("vector", []))
        index = hnswlib.Index(space=self.space, dim=dimension)
        index.load_index(str(index_path), max_elements=len(ordered_ids))
        index.set_ef(max(self.ef_search, limit * max(probe_count, 1)))
        labels, _ = index.knn_query(np.array([query_vector], dtype="float32"), k=min(len(ordered_ids), limit * max(probe_count, 1)))
        ranked: list[str] = []
        for item_index in labels[0]:
            if item_index < 0 or item_index >= len(ordered_ids):
                continue
            item_id = ordered_ids[int(item_index)]
            if item_id in allowed and item_id not in ranked:
                ranked.append(item_id)
        if len(ranked) >= limit:
            return ranked[:limit]
        ranked.extend(
            item_id
            for item_id in self._fallback_rank(entries, allowed, query_vector, exclude=set(ranked), limit=limit - len(ranked))
            if item_id not in ranked
        )
        return ranked[:limit]

    def _fallback_rank(
        self,
        entries: dict[str, dict[str, Any]],
        allowed: set[str],
        query_vector: list[float],
        *,
        exclude: set[str],
        limit: int,
    ) -> list[str]:
        ranked: list[tuple[float, str]] = []
        for item_id, entry in entries.items():
            if item_id not in allowed or item_id in exclude:
                continue
            ranked.append((cosine_similarity(query_vector, [float(value) for value in entry.get("vector", [])]), item_id))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item_id for _, item_id in ranked[:limit]]


class MilvusVectorStore(BaseVectorStore):
    backend_name = "milvus"

    def __init__(self, config: RetrievalConfig) -> None:
        self.config = config
        try:
            from pymilvus import MilvusClient  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VectorStoreError("Milvus backend requires `pymilvus`.") from exc

    def ensure_documents(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        embedding_client: EmbeddingClient,
        *,
        signature_length: int,
    ) -> dict[str, list[float]]:
        client = self._client()
        vectors = embedding_client.embed_texts([str(document.get("text", "")) for document in documents]) if documents else []
        if not documents:
            return {}
        dimension = len(vectors[0]) if vectors else EmbeddingClient.LOCAL_DIMENSION
        collection_name = self._collection_name(collection)
        if not client.has_collection(collection_name):  # pragma: no cover - integration path
            client.create_collection(
                collection_name=collection_name,
                dimension=dimension,
                primary_field_name="item_id",
                id_type="string",
                vector_field_name="embedding",
                metric_type="COSINE",
            )
        records = [
            {
                "item_id": str(document["item_id"]),
                "text_hash": self._text_hash(str(document.get("text", ""))),
                "payload": json.dumps(document.get("payload", {}), ensure_ascii=False),
                "embedding": vector,
            }
            for document, vector in zip(documents, vectors)
        ]
        client.upsert(collection_name=collection_name, data=records)  # pragma: no cover - integration path
        return {str(document["item_id"]): vector for document, vector in zip(documents, vectors)}

    def ann_search(
        self,
        collection: str,
        item_ids: list[str],
        query_vector: list[float],
        *,
        signature_length: int,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        client = self._client()
        collection_name = self._collection_name(collection)
        filter_expr = f"item_id in {json.dumps(item_ids)}" if item_ids else ""
        results = client.search(  # pragma: no cover - integration path
            collection_name=collection_name,
            data=[query_vector],
            anns_field="embedding",
            limit=max(limit * max(probe_count, 1), limit),
            filter=filter_expr or None,
            output_fields=["item_id"],
        )
        matches: list[str] = []
        for hit in results[0] if results else []:
            entity = hit.get("entity", {})
            item_id = str(entity.get("item_id", ""))
            if item_id and item_id not in matches:
                matches.append(item_id)
        return matches[: max(limit * max(probe_count, 1), limit)]

    def _client(self):
        from pymilvus import MilvusClient

        return MilvusClient(uri=self.config.milvus_uri, token=self.config.milvus_token)

    def _collection_name(self, collection: str) -> str:
        return f"{self.config.milvus_collection_prefix}{re.sub(r'[^A-Za-z0-9_]+', '_', collection)}"

    def _text_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PgVectorStore(BaseVectorStore):
    backend_name = "pgvector"

    def __init__(self, config: RetrievalConfig) -> None:
        if not config.pgvector_dsn:
            raise VectorStoreError("PGVector backend requires `PGVECTOR_DSN`.")
        self.config = config
        self.table_name = self._sanitize_table_name(config.pgvector_table)
        try:
            import psycopg  # noqa: F401
            from pgvector.psycopg import register_vector  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VectorStoreError("PGVector backend requires `psycopg` and `pgvector`.") from exc
        self._init_db()

    def ensure_documents(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        embedding_client: EmbeddingClient,
        *,
        signature_length: int,
    ) -> dict[str, list[float]]:
        if not documents:
            return {}
        import psycopg
        from pgvector.psycopg import register_vector

        vectors = embedding_client.embed_texts([str(document.get("text", "")) for document in documents])
        with psycopg.connect(self.config.pgvector_dsn) as connection:  # pragma: no cover - integration path
            register_vector(connection)
            with connection.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO {self.table_name} (collection, item_id, text_hash, payload, embedding)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (collection, item_id)
                    DO UPDATE SET
                        text_hash = EXCLUDED.text_hash,
                        payload = EXCLUDED.payload,
                        embedding = EXCLUDED.embedding
                    """,
                    [
                        (
                            collection,
                            str(document["item_id"]),
                            hashlib.sha256(str(document.get("text", "")).encode("utf-8")).hexdigest(),
                            json.dumps(document.get("payload", {}), ensure_ascii=False),
                            vector,
                        )
                        for document, vector in zip(documents, vectors)
                    ],
                )
            connection.commit()
        return {str(document["item_id"]): vector for document, vector in zip(documents, vectors)}

    def ann_search(
        self,
        collection: str,
        item_ids: list[str],
        query_vector: list[float],
        *,
        signature_length: int,
        probe_count: int,
        limit: int,
    ) -> list[str]:
        import psycopg
        from pgvector.psycopg import register_vector

        with psycopg.connect(self.config.pgvector_dsn) as connection:  # pragma: no cover - integration path
            register_vector(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT item_id
                    FROM {self.table_name}
                    WHERE collection = %s AND item_id = ANY(%s)
                    ORDER BY embedding <=> %s
                    LIMIT %s
                    """,
                    (
                        collection,
                        item_ids,
                        query_vector,
                        max(limit * max(probe_count, 1), limit),
                    ),
                )
                rows = cursor.fetchall()
        return [str(item_id) for (item_id,) in rows]

    def _init_db(self) -> None:
        import psycopg

        with psycopg.connect(self.config.pgvector_dsn) as connection:  # pragma: no cover - integration path
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.table_name} (
                        collection TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        text_hash TEXT NOT NULL,
                        payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding vector,
                        PRIMARY KEY (collection, item_id)
                    )
                    """
                )
            connection.commit()

    def _sanitize_table_name(self, raw_name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_")
        if not cleaned:
            raise VectorStoreError("Invalid PGVector table name.")
        return cleaned


def build_vector_store(config: RetrievalConfig, storage_path: str | Path) -> BaseVectorStore:
    preferred = config.vector_backend.strip().lower()
    fallback = config.vector_backend_fallback.strip().lower()
    candidates = _backend_candidates(preferred, fallback)
    last_error: Exception | None = None
    for name in candidates:
        try:
            if name == "sqlite_ann":
                return SQLiteVectorStore(storage_path)
            if name == "faiss_flat":
                return FaissVectorStore(storage_path, use_hnsw=False, hnsw_m=config.hnsw_m)
            if name == "faiss_hnsw":
                return FaissVectorStore(storage_path, use_hnsw=True, hnsw_m=config.hnsw_m)
            if name == "hnswlib":
                return HnswlibVectorStore(
                    storage_path,
                    space=config.hnsw_space,
                    m=config.hnsw_m,
                    ef_construction=config.hnsw_ef_construction,
                    ef_search=config.hnsw_ef_search,
                )
            if name == "milvus":
                return MilvusVectorStore(config)
            if name == "pgvector":
                return PgVectorStore(config)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    if last_error is not None:
        return SQLiteVectorStore(storage_path)
    raise VectorStoreError("No vector backend candidates were available.")


def _backend_candidates(preferred: str, fallback: str) -> list[str]:
    if preferred == "auto":
        ordered = ["faiss_flat", "hnswlib", "faiss_hnsw", fallback or "sqlite_ann", "sqlite_ann"]
    else:
        ordered = [preferred, fallback or "sqlite_ann", "sqlite_ann"]
    seen: set[str] = set()
    result: list[str] = []
    for item in ordered:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
