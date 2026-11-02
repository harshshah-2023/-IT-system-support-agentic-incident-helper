"""Semantic router: classifies a query with no LLM call using nearest labelled utterances.

Cost and latency of routing must be far below the call being routed, so a local embedding comparison
decides. Only when the two classes are nearly tied does the optional `tiebreak` model get asked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum

import numpy as np
from pydantic import BaseModel, Field


class Route(StrEnum):
    SIMPLE_FAQ = "simple_faq"
    COMPLEX_REASONING = "complex_reasoning"


class Intent(StrEnum):
    FAQ = "faq"
    REASONING = "reasoning"
    CODING = "coding"
    SUMMARIZATION = "summarization"


class RouteDecision(BaseModel):
    route: Route
    intent: Intent
    confidence: float
    method: str = "semantic"


class TiebreakVerdict(BaseModel):
    """Choose how demanding a user query is for an IT operations assistant."""

    route: Route = Field(description="simple_faq for a lookup or status check, otherwise complex_reasoning")
    reasoning: str = Field(description="One sentence justification")


UTTERANCES: dict[Intent, list[str]] = {
    Intent.FAQ: [
        "Is the login service up?",
        "What is the status of payments-api?",
        "Who is the DBA lead?",
        "What version is checkout running?",
        "How many instances of auth-service are healthy?",
        "Which team owns orders-api?",
        "What is the acknowledge time for a SEV1?",
        "Is the database healthy?",
    ],
    Intent.REASONING: [
        "Why did payments fail after the 2 a.m. release?",
        "What is the root cause of the login failures?",
        "Why are payments slow after last night's release?",
        "Analyse how the deployment and the database pressure relate to the outage.",
        "Should we roll back or fix forward given these symptoms?",
        "Compare the likely causes of this outage and recommend a mitigation plan.",
        "Diagnose why checkout errors rose while the database looks fine.",
        "Investigate what caused last night's outage and how we stop it recurring.",
    ],
    Intent.CODING: [
        "Write a script that checks replication lag.",
        "Fix this SQL query that lists replication slots.",
        "Generate a Python function to parse these log lines.",
    ],
    Intent.SUMMARIZATION: [
        "Summarise this incident for the status page.",
        "Give me a short recap of the timeline.",
        "Condense these alerts into two lines.",
    ],
}

ROUTE_OF_INTENT = {
    Intent.FAQ: Route.SIMPLE_FAQ,
    Intent.SUMMARIZATION: Route.SIMPLE_FAQ,
    Intent.REASONING: Route.COMPLEX_REASONING,
    Intent.CODING: Route.COMPLEX_REASONING,
}


class SemanticRouter:
    def __init__(
        self,
        embed_many: Callable[[list[str]], Sequence[Sequence[float]]],
        embed_one: Callable[[str], Sequence[float]],
        tiebreak: Callable[[str], Route] | None = None,
        margin: float = 0.02,
    ) -> None:
        self._embed_one = embed_one
        self._tiebreak = tiebreak
        self._margin = margin
        self._labels: list[Intent] = []
        texts: list[str] = []
        for intent, samples in UTTERANCES.items():
            self._labels += [intent] * len(samples)
            texts += samples
        matrix = np.asarray(embed_many(texts), dtype=float)
        self._matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    def classify(self, query: str) -> RouteDecision:
        vector = np.asarray(self._embed_one(query), dtype=float)
        vector /= np.linalg.norm(vector) or 1.0
        scores = self._matrix @ vector
        best_per_intent = {
            intent: float(max(s for s, label in zip(scores, self._labels, strict=True) if label == intent))
            for intent in UTTERANCES
        }
        intent = max(best_per_intent, key=lambda key: best_per_intent[key])
        route = ROUTE_OF_INTENT[intent]
        simple = max(v for k, v in best_per_intent.items() if ROUTE_OF_INTENT[k] is Route.SIMPLE_FAQ)
        complex_ = max(v for k, v in best_per_intent.items() if ROUTE_OF_INTENT[k] is Route.COMPLEX_REASONING)
        margin = abs(simple - complex_)
        if margin < self._margin:
            if self._tiebreak is not None:
                return RouteDecision(
                    route=self._tiebreak(query),
                    intent=intent,
                    confidence=round(margin, 3),
                    method="llm_tiebreak",
                )
            route = Route.COMPLEX_REASONING  # when unsure, prefer the correct answer over the cheap one
        return RouteDecision(route=route, intent=intent, confidence=round(margin, 3))
