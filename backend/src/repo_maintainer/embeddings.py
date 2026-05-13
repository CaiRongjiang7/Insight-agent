from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import EmbeddingConfig


class EmbeddingError(RuntimeError):
    pass


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


class VectorCache:
    def __init__(self, cache_path: str | Path) -> None:
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, list[float]] = {}
        self._load()

    def get(self, key: str) -> list[float] | None:
        return self._data.get(key)

    def set(self, key: str, vector: list[float]) -> None:
        self._data[key] = vector
        self._save()

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            self._data = {str(key): [float(item) for item in value] for key, value in payload.items() if isinstance(value, list)}

    def _save(self) -> None:
        self.cache_path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")


class EmbeddingClient:
    LOCAL_DIMENSION = 128

    def __init__(self, config: EmbeddingConfig, vector_cache: VectorCache) -> None:
        self.config = config
        self.vector_cache = vector_cache

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float] | None] = []
        pending: list[tuple[int, str, str]] = []
        for index, text in enumerate(texts):
            cache_key = self._cache_key(text)
            cached = self.vector_cache.get(cache_key)
            if cached is not None:
                vectors.append(cached)
                continue
            vectors.append(None)
            pending.append((index, text, cache_key))

        if pending:
            if self._should_use_remote():
                try:
                    generated = self._remote_embeddings([text for _, text, _ in pending])
                except (EmbeddingError, OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
                    generated = [self._local_embedding(text) for _, text, _ in pending]
            else:
                generated = [self._local_embedding(text) for _, text, _ in pending]
            for (index, _, cache_key), vector in zip(pending, generated):
                self.vector_cache.set(cache_key, vector)
                vectors[index] = vector

        return [vector or self._local_embedding(texts[index]) for index, vector in enumerate(vectors)]

    def _should_use_remote(self) -> bool:
        return self.config.enabled and bool(self.config.api_key) and bool(self.config.model)

    def _remote_embeddings(self, texts: list[str]) -> list[list[float]]:
        mode = self.config.compatibility_mode.strip().lower()
        if mode == "openai_compatible":
            return self._openai_compatible_embeddings(texts)
        if mode == "google_gemini":
            return self._google_gemini_embeddings(texts)
        raise EmbeddingError(f"Unsupported embedding compatibility mode: {self.config.compatibility_mode}")

    def _openai_compatible_embeddings(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.config.model,
            "input": texts,
        }
        endpoint = self.config.base_url.rstrip("/") + "/embeddings"
        headers = {
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        result = self._post_json(endpoint, payload, headers)
        data = result.get("data", [])
        vectors: list[list[float]] = []
        for item in data:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingError("Embeddings API returned malformed vectors.")
            vectors.append([float(value) for value in embedding])
        if len(vectors) != len(texts):
            raise EmbeddingError("Embeddings API returned an unexpected number of vectors.")
        return vectors

    def _google_gemini_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not self.config.api_key:
            raise EmbeddingError("Google Gemini provider requires an API key.")
        vectors: list[list[float]] = []
        for text in texts:
            query = urlencode({"key": self.config.api_key})
            endpoint = f"{self.config.base_url.rstrip('/')}/models/{self.config.model}:embedContent?{query}"
            payload = {
                "content": {
                    "parts": [{"text": text}],
                }
            }
            result = self._post_json(endpoint, payload, {"Content-Type": "application/json", **self.config.extra_headers})
            vector = result.get("embedding", {}).get("values", [])
            if not isinstance(vector, list):
                raise EmbeddingError("Google Gemini embeddings API returned malformed vectors.")
            vectors.append([float(value) for value in vector])
        return vectors

    def _post_json(self, endpoint: str, payload: dict, headers: dict[str, str]) -> dict:
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise EmbeddingError(f"Embeddings API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise EmbeddingError(f"Failed to reach Embeddings API: {exc}") from exc
        except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException) as exc:
            raise EmbeddingError(f"Embeddings API request failed: {exc}") from exc

    def _local_embedding(self, text: str) -> list[float]:
        vector = [0.0] * self.LOCAL_DIMENSION
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{1,}", text.lower())
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:2], "big") % self.LOCAL_DIMENSION
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            weight = 1.0 + (digest[3] / 255.0)
            vector[bucket] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def _cache_key(self, text: str) -> str:
        cache_context = "\n".join(
            [
                self.config.provider,
                self.config.model,
                self.config.base_url.rstrip("/"),
                self.config.compatibility_mode,
                text,
            ]
        )
        return hashlib.sha256(cache_context.encode("utf-8")).hexdigest()
