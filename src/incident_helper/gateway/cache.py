"""Two-tier prompt cache.

Exact tier: LangChain's `InMemoryCache` in front of `SQLiteCache`, keyed on the normalised request.
Semantic tier: embedding similarity, guarded so that "Is login up?" can never answer "Is payments up?".

Only prompts with no tools bound are cached. Live operational answers depend on state that changes,
so tool-loop turns go to the model every time, and every entry carries a TTL.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from langchain_community.cache import SQLiteCache
from langchain_core.caches import InMemoryCache
from langchain_core.outputs import Generation

# Tokens that change the meaning of a question while hardly moving its embedding.
_ENTITY = re.compile(r"\b(?:[a-z]+-(?:api|db|web|service|cache|scorer|gateway)|\d[\d.]*|sev\d|inc-\d+)\b")
_SERVICE_WORDS = ("login", "auth", "payment", "checkout", "order", "ledger", "database", "disk", "fraud")


def entities(text: str) -> frozenset[str]:
    lowered = text.lower()
    found = set(_ENTITY.findall(lowered))
    found.update(word for word in _SERVICE_WORDS if word in lowered)
    return frozenset(found)


def normalise(text: str) -> str:
    return " ".join(text.lower().split())


@dataclass(slots=True)
class CacheHit:
    text: str
    kind: str  # "exact" | "semantic"


class PromptCache:
    def __init__(
        self,
        path: Path,
        embed: Callable[[str], Sequence[float]] | None = None,
        ttl_seconds: int = 900,
        semantic_threshold: float = 0.93,
        clock: Callable[[], float] = time.time,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = InMemoryCache(maxsize=2048)
        self._disk = SQLiteCache(database_path=str(path))
        self._embed = embed
        self._ttl = ttl_seconds
        self._threshold = semantic_threshold
        self._clock = clock
        self._entries: list[tuple[np.ndarray, frozenset[str], str, float, str]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _key(prompt: str) -> str:
        return hashlib.sha256(normalise(prompt).encode()).hexdigest()

    def _fresh(self, generations: Sequence[Generation] | None) -> str | None:
        if not generations:
            return None
        payload = json.loads(generations[0].text)
        return payload["text"] if self._clock() - payload["at"] <= self._ttl else None

    def lookup_exact(self, prompt: str, namespace: str) -> CacheHit | None:
        key = self._key(prompt)
        cached = self._memory.lookup(key, namespace)
        text = self._fresh(cached)
        if text is None:
            cached = self._disk.lookup(key, namespace)
            text = self._fresh(cached)
            if text is not None and cached:
                self._memory.update(key, namespace, cached)  # promote to the fast tier
        return CacheHit(text, "exact") if text is not None else None

    def lookup_semantic(self, prompt: str, namespace: str = "") -> CacheHit | None:
        if self._embed is None:
            return None
        query = np.asarray(self._embed(prompt), dtype=float)
        query /= np.linalg.norm(query) or 1.0
        wanted = entities(prompt)
        now = self._clock()
        best: tuple[float, str] | None = None
        with self._lock:
            for vector, tags, text, at, entry_namespace in self._entries:
                if entry_namespace != namespace or tags != wanted or now - at > self._ttl:
                    continue
                score = float(vector @ query)
                if score >= self._threshold and (best is None or score > best[0]):
                    best = (score, text)
        return CacheHit(best[1], "semantic") if best else None

    def store(self, prompt: str, namespace: str, text: str) -> None:
        generations = [Generation(text=json.dumps({"text": text, "at": self._clock()}))]
        key = self._key(prompt)
        self._memory.update(key, namespace, generations)
        self._disk.update(key, namespace, generations)
        if self._embed is not None:
            vector = np.asarray(self._embed(prompt), dtype=float)
            vector /= np.linalg.norm(vector) or 1.0
            with self._lock:
                self._entries.append((vector, entities(prompt), text, self._clock(), namespace))

    def clear(self) -> None:
        self._memory.clear()
        self._disk.clear()
        with self._lock:
            self._entries.clear()
