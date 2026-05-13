"""
语义检索器（重构版）

新增能力：
1. BM25 关键词召回（混合检索：向量 + BM25 → rerank）
2. 知识库条目检索（KnowledgeEntry 类型支持）
3. 混合分数归一化（将 BM25 分数归一化到 [0,1] 与向量分数对齐）
"""
from __future__ import annotations

import re
from typing import Any

from .bm25_index import BM25Index, BM25SearchResult
from .config import RetrievalConfig
from .embeddings import EmbeddingClient, cosine_similarity
from .models import KnowledgeEntry, RetrievalMatch, Skill
from .reranker import BaseReranker, build_reranker
from .vector_store import BaseVectorStore


class SemanticRetriever:
    """
    语义检索器（混合检索：向量语义 + BM25 关键词 + reranker）

    召回流程：
    1. BM25 关键词召回（精确匹配技术词汇、错误码等）
    2. 向量语义召回（捕捉语义相似但关键词不同的文档）
    3. 合并去重 + rerank（综合质量排序）
    4. MMR 多样化（避免结果同质化）
    """

    def __init__(
        self,
        config: RetrievalConfig,
        embedding_client: EmbeddingClient,
        vector_store: BaseVectorStore | None = None,
        reranker: BaseReranker | None = None,
        bm25_index: BM25Index | None = None,
    ) -> None:
        self.config = config
        self.embedding_client = embedding_client
        self.vector_store = vector_store
        self.reranker = reranker or build_reranker(config)
        self.bm25_index = bm25_index   # 可选的 BM25 索引（阶段四新增）

    def rank_skills(
        self,
        query_text: str,
        skills: list[Skill | KnowledgeEntry],
        limit: int | None = None,
    ) -> list[RetrievalMatch]:
        """排名知识条目（支持 Skill 和 KnowledgeEntry 两种类型）"""
        documents = [
            {
                "item_id": skill.id,
                "item_type": "knowledge_entry",
                "text": " ".join([
                    skill.name,
                    skill.description,
                    " ".join(skill.triggers),
                    skill.action_template,
                    " ".join(getattr(skill, "keywords", [])),
                ]),
                "success_rate": skill.success_rate,
                "payload": {
                    "name": skill.name,
                    "description": skill.description,
                    "success_rate": skill.success_rate,
                    "error_types": skill.error_types,
                    "keywords": getattr(skill, "keywords", []),
                },
            }
            for skill in skills
            if skill.active
        ]
        return self._rank_documents(query_text, documents, limit or self.config.top_k, collection="skills")

    # 向后兼容别名
    def rank_knowledge_entries(
        self,
        query_text: str,
        entries: list[KnowledgeEntry | Skill],
        limit: int | None = None,
    ) -> list[RetrievalMatch]:
        """排名知识条目（rank_skills 的新名称）"""
        return self.rank_skills(query_text, entries, limit)

    def rank_memories(self, query_text: str, memories: list[dict[str, Any]], limit: int | None = None) -> list[RetrievalMatch]:
        documents = [
            {
                "item_id": memory.get("memory_id") or memory.get("trace_id", f"memory-{index}"),
                "item_type": "memory",
                "text": " ".join(
                    [
                        str(memory.get("incident_type", "")),
                        str(memory.get("cluster", "")),
                        str(memory.get("role", "")),
                        str(memory.get("repair_summary", "")),
                    ]
                ),
                "success_rate": 1.0 if memory.get("success", True) else 0.0,
                "payload": memory,
            }
            for index, memory in enumerate(memories[: self.config.max_memory_candidates])
        ]
        return self._rank_documents(query_text, documents, limit or self.config.top_k, collection="memories")

    def rank_with_bm25(
        self,
        query_text: str,
        doc_types: list[str] | None = None,
        vector_candidates: list[dict[str, Any]] | None = None,
        limit: int = 10,
        bm25_weight: float = 0.3,
    ) -> list[RetrievalMatch]:
        """
        BM25 + 向量语义混合召回

        Args:
            query_text: 搜索查询
            doc_types: 文档类型过滤
            vector_candidates: 已有的向量候选文档（可选，没有时仅返回 BM25 结果）
            limit: 返回数量
            bm25_weight: BM25 分数权重（其余为向量分数权重）

        Returns:
            合并后的 RetrievalMatch 列表
        """
        if self.bm25_index is None:
            # 没有 BM25 索引时降级为纯向量检索
            if vector_candidates:
                return self._rank_documents(
                    query_text, vector_candidates, limit, collection="mixed"
                )
            return []

        # BM25 召回
        bm25_results = self.bm25_index.search(query_text, doc_types=doc_types, limit=limit * 2)

        if not bm25_results and not vector_candidates:
            return []

        # 归一化 BM25 分数到 [0,1]
        bm25_normalized = self._normalize_bm25_scores(bm25_results)

        # 合并 BM25 结果与向量候选
        merged: dict[str, dict[str, Any]] = {}
        for result, normalized_score in zip(bm25_results, bm25_normalized):
            merged[result.doc_id] = {
                "item_id": result.doc_id,
                "item_type": result.doc_type,
                "text": result.title + " " + " ".join(result.matched_terms),
                "success_rate": 0.0,
                "bm25_score": normalized_score,
                "payload": {**result.metadata, "title": result.title},
            }

        if vector_candidates:
            for doc in vector_candidates:
                doc_id = str(doc["item_id"])
                if doc_id in merged:
                    # 已有 BM25 结果，保留 BM25 分数
                    merged[doc_id].update({
                        "text": doc.get("text", merged[doc_id]["text"]),
                        "success_rate": doc.get("success_rate", 0.0),
                        "payload": doc.get("payload", merged[doc_id]["payload"]),
                    })
                else:
                    merged[doc_id] = {**doc, "bm25_score": 0.0}

        # 统一进行向量评分
        combined_docs = list(merged.values())
        vector_matches = self._rank_documents(
            query_text, combined_docs, limit * 2, collection="mixed"
        )

        # 融合 BM25 分数
        final_matches: list[RetrievalMatch] = []
        for match in vector_matches:
            doc = merged.get(match.item_id, {})
            bm25_score = float(doc.get("bm25_score", 0.0))
            fused_score = (
                (1.0 - bm25_weight) * match.score
                + bm25_weight * bm25_score
            )
            match.score = round(fused_score, 6)
            final_matches.append(match)

        final_matches.sort(key=lambda m: m.score, reverse=True)
        return final_matches[:limit]

    def _normalize_bm25_scores(self, results: list[BM25SearchResult]) -> list[float]:
        """将 BM25 分数归一化到 [0,1]"""
        if not results:
            return []
        max_score = max(r.score for r in results)
        if max_score <= 0:
            return [0.0] * len(results)
        return [r.score / max_score for r in results]

    def _rank_documents(
        self,
        query_text: str,
        documents: list[dict[str, Any]],
        limit: int,
        *,
        collection: str,
    ) -> list[RetrievalMatch]:
        if not documents:
            return []
        query_terms = set(self._tokenize(query_text))
        candidate_documents = self._candidate_pool(query_terms, documents)
        query_vector = self.embedding_client.embed_text(query_text)
        ann_candidates = candidate_documents
        if self.vector_store is not None:
            document_vectors = self.vector_store.ensure_documents(
                collection,
                candidate_documents,
                self.embedding_client,
                signature_length=self.config.ann_signature_length,
            )
            if self.config.ann_enabled and len(candidate_documents) > max(limit * 2, 6):
                ann_ids = self.vector_store.ann_search(
                    collection,
                    [str(document["item_id"]) for document in candidate_documents],
                    query_vector,
                    signature_length=self.config.ann_signature_length,
                    probe_count=self.config.ann_probe_count,
                    limit=limit,
                )
                ann_set = set(ann_ids)
                ann_candidates = [document for document in candidate_documents if str(document["item_id"]) in ann_set] or candidate_documents
        else:
            generated = self.embedding_client.embed_texts([document["text"] for document in candidate_documents])
            document_vectors = {
                str(document["item_id"]): vector
                for document, vector in zip(candidate_documents, generated)
            }
        reranker_scores = self.reranker.score_batch(query_text, ann_candidates) if self.config.reranker_enabled else [0.0] * len(ann_candidates)
        matches: list[RetrievalMatch] = []
        token_sets: dict[str, set[str]] = {}
        for index, document in enumerate(ann_candidates):
            vector = document_vectors.get(str(document["item_id"]), [])
            document_terms = self._tokenize(document["text"])
            token_sets[str(document["item_id"])] = set(document_terms)
            semantic_score = max(cosine_similarity(query_vector, vector), 0.0)
            lexical_score = self._lexical_overlap(query_terms, document_terms)
            success_bonus = float(document.get("success_rate", 0.0))
            base_score = (
                semantic_score * self.config.semantic_weight
                + lexical_score * self.config.lexical_weight
                + success_bonus * self.config.success_weight
                + self._document_alignment_bonus(query_terms, document)
            )
            reranker_score = reranker_scores[index] if index < len(reranker_scores) else 0.0
            score = (
                base_score if not self.config.reranker_enabled
                else ((1.0 - self.config.reranker_weight) * base_score) + (self.config.reranker_weight * reranker_score)
            )
            if score < self.config.min_combined_score and semantic_score <= 0.0 and lexical_score <= 0.0 and reranker_score <= 0.0:
                continue
            matches.append(
                RetrievalMatch(
                    item_id=str(document["item_id"]),
                    item_type=str(document["item_type"]),
                    score=round(score, 6),
                    lexical_score=round(lexical_score, 6),
                    semantic_score=round(semantic_score, 6),
                    reranker_score=round(reranker_score, 6),
                    payload=dict(document["payload"]),
                )
            )
        matches.sort(key=lambda match: match.score, reverse=True)
        return self._diversify(matches, token_sets, limit)

    def _candidate_pool(self, query_terms: set[str], documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(documents) <= self.config.candidate_pool_size:
            return documents
        ranked = []
        for document in documents:
            document_terms = self._tokenize(document["text"])
            lexical_score = self._lexical_overlap(query_terms, document_terms)
            bootstrap_score = lexical_score + (float(document.get("success_rate", 0.0)) * 0.05)
            ranked.append((bootstrap_score, document))
        ranked.sort(key=lambda item: item[0], reverse=True)
        pool = [document for _, document in ranked[: self.config.candidate_pool_size]]
        if not any(score > 0 for score, _ in ranked[: self.config.candidate_pool_size]):
            return documents[: self.config.candidate_pool_size]
        return pool

    def _diversify(self, matches: list[RetrievalMatch], token_sets: dict[str, set[str]], limit: int) -> list[RetrievalMatch]:
        if len(matches) <= limit:
            return matches
        selected: list[RetrievalMatch] = []
        remaining = list(matches)
        while remaining and len(selected) < limit:
            best_match = None
            best_score = float("-inf")
            for match in remaining:
                redundancy = 0.0
                if selected:
                    redundancy = max(
                        self._set_similarity(token_sets.get(match.item_id, set()), token_sets.get(item.item_id, set()))
                        for item in selected
                    )
                mmr_score = (self.config.mmr_lambda * match.score) - ((1.0 - self.config.mmr_lambda) * redundancy)
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_match = match
            if best_match is None:
                break
            selected.append(best_match)
            remaining = [item for item in remaining if item.item_id != best_match.item_id]
        return selected

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{1,}", text.lower())

    def _lexical_overlap(self, query_terms: set[str], document_terms: list[str]) -> float:
        if not query_terms or not document_terms:
            return 0.0
        doc_set = set(document_terms)
        overlap = len(query_terms & doc_set)
        return overlap / max(len(query_terms), 1)

    def _set_similarity(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / max(len(left | right), 1)

    def _document_alignment_bonus(self, query_terms: set[str], document: dict[str, Any]) -> float:
        payload = document.get("payload", {})
        if not isinstance(payload, dict):
            return 0.0
        bonus = 0.0
        item_type = document.get("item_type", "")
        if item_type in ("skill", "knowledge_entry"):
            error_types = {str(item).lower() for item in payload.get("error_types", [])}
            name_terms = set(self._tokenize(str(payload.get("name", ""))))
            kw_terms = set(self._tokenize(" ".join(payload.get("keywords", []))))
            if query_terms & error_types:
                bonus += self.config.error_type_boost
            if len(query_terms & name_terms) >= 2:
                bonus += self.config.cluster_boost
            # 关键词命中额外奖励
            if query_terms & kw_terms:
                bonus += 0.05
            return bonus

        incident_type = str(payload.get("incident_type", "")).lower()
        cluster_terms = set(self._tokenize(str(payload.get("cluster", ""))))
        if incident_type and incident_type in query_terms:
            bonus += self.config.error_type_boost
        if query_terms & cluster_terms:
            bonus += self.config.cluster_boost
        return bonus
