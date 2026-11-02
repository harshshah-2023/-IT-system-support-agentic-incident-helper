"""Local dense embeddings (ONNX via FastEmbed), shared by vector RAG, the router and the semantic cache.

Running locally keeps embeddings free, private (runbooks stay on the machine) and independent of the
LLM provider, so a provider outage cannot take down retrieval or routing.
"""

from __future__ import annotations

import threading

from fastembed import TextEmbedding
from langchain_core.embeddings import Embeddings

from incident_helper.config import get_settings


class LocalEmbeddings(Embeddings):
    def __init__(self, model_name: str | None = None) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self._model = TextEmbedding(self.model_name, cache_dir=str(settings.var_dir / "models"))
        self._lock = threading.Lock()  # the ONNX session is shared across threads

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [vector.tolist() for vector in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            return next(iter(self._model.query_embed(text))).tolist()


_instance: LocalEmbeddings | None = None
_instance_lock = threading.Lock()


def get_embeddings() -> LocalEmbeddings:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = LocalEmbeddings()
        return _instance
