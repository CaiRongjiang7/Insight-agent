from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .config import RetrievalConfig


class BaseReranker:
    backend_name = "heuristic"

    def score_batch(self, query_text: str, documents: list[dict[str, Any]]) -> list[float]:
        raise NotImplementedError


class HeuristicReranker(BaseReranker):
    backend_name = "heuristic"

    def __init__(self, config: RetrievalConfig) -> None:
        self.config = config

    def score_batch(self, query_text: str, documents: list[dict[str, Any]]) -> list[float]:
        return [self._score(query_text, str(document.get("text", "")), dict(document.get("payload", {}))) for document in documents]

    def _score(self, query_text: str, document_text: str, payload: dict[str, Any]) -> float:
        query_tokens = self._tokenize(query_text)[: self.config.reranker_query_window]
        document_tokens = self._tokenize(document_text)
        if not query_tokens or not document_tokens:
            return 0.0
        query_set = set(query_tokens)
        doc_set = set(document_tokens)
        overlap_score = len(query_set & doc_set) / max(len(query_set), 1)
        ordered_hits = 0
        last_index = -1
        for token in query_tokens:
            try:
                index = document_tokens.index(token, last_index + 1)
            except ValueError:
                continue
            ordered_hits += 1
            last_index = index
        order_score = ordered_hits / max(len(query_tokens), 1)
        cluster_terms = self._tokenize(str(payload.get("cluster", "")))
        cluster_score = 1.0 if set(cluster_terms) & query_set else 0.0
        summary_terms = self._tokenize(str(payload.get("repair_summary", payload.get("description", ""))))
        summary_overlap = len(set(summary_terms) & query_set) / max(len(query_set), 1)
        return round((overlap_score * 0.45) + (order_score * 0.3) + (cluster_score * 0.15) + (summary_overlap * 0.1), 6)

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{1,}", text.lower())


class CrossEncoderReranker(BaseReranker):
    backend_name = "cross_encoder"

    def __init__(self, config: RetrievalConfig) -> None:
        self.config = config
        self._model = None
        self._fallback = HeuristicReranker(config)

    def score_batch(self, query_text: str, documents: list[dict[str, Any]]) -> list[float]:
        if not documents:
            return []
        try:
            model = self._load_model()
            pairs = [(query_text, str(document.get("text", ""))) for document in documents]
            raw_scores = model.predict(pairs, batch_size=max(self.config.reranker_batch_size, 1), show_progress_bar=False)
        except Exception:  # noqa: BLE001
            return self._fallback.score_batch(query_text, documents)
        normalized: list[float] = []
        for value in raw_scores:
            score = float(value)
            if score < 0.0 or score > 1.0:
                score = 1.0 / (1.0 + math.exp(-score))
            normalized.append(round(score, 6))
        return normalized

    def _load_model(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self.config.reranker_model, device=self.config.reranker_device)
        return self._model

    @staticmethod
    def model_is_cached(model_name: str) -> bool:
        normalized = model_name.replace("/", "--")
        candidate = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{normalized}"
        return candidate.exists()


def build_reranker(config: RetrievalConfig) -> BaseReranker:
    backend = config.reranker_backend.strip().lower()
    if backend in {"cross_encoder", "neural"}:
        try:
            return CrossEncoderReranker(config)
        except Exception:  # pragma: no cover - environment dependent
            raise
    if backend == "auto" and CrossEncoderReranker.model_is_cached(config.reranker_model):
        try:
            return CrossEncoderReranker(config)
        except Exception:  # pragma: no cover - environment dependent
            pass
    return HeuristicReranker(config)
