"""Structured FinOps telemetry: one record per gateway call, logged, kept in memory and on disk."""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("incident_helper.finops")


@dataclass(slots=True)
class TelemetryRecord:
    model_used: str
    fallback_triggered: bool
    cache_hit: bool
    latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    # Context beyond the mandatory assessment keys.
    tier: str = ""
    route: str = ""
    purpose: str = ""
    cache_kind: str | None = None
    attempts: list[str] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinOpsLedger:
    def __init__(self, path: Path | None = None, echo: bool = False) -> None:
        self.path = path
        self.echo = echo
        self._records: list[TelemetryRecord] = []
        self._lock = threading.Lock()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: TelemetryRecord) -> None:
        line = json.dumps(entry.to_dict())
        with self._lock:
            self._records.append(entry)
            if self.path:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        logger.info(line)
        if self.echo:
            print(line)

    @property
    def records(self) -> list[TelemetryRecord]:
        with self._lock:
            return list(self._records)

    def persisted(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.path or not self.path.exists():
            return [r.to_dict() for r in self.records[-limit:]]
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines]

    def summary(self) -> dict[str, Any]:
        rows = self.persisted(limit=100_000)
        by_model: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_ms": 0.0}
        )
        for row in rows:
            bucket = by_model[row["model_used"]]
            bucket["calls"] += 1
            bucket["input_tokens"] += row["input_tokens"]
            bucket["output_tokens"] += row["output_tokens"]
            bucket["cost_usd"] += row["estimated_cost_usd"] or 0.0
            bucket["latency_ms"] += row["latency_ms"]
        for bucket in by_model.values():
            bucket["avg_latency_ms"] = round(bucket.pop("latency_ms") / bucket["calls"], 1)
            bucket["cost_usd"] = round(bucket["cost_usd"], 6)
        return {
            "calls": len(rows),
            "cache_hits": sum(1 for r in rows if r["cache_hit"]),
            "fallbacks": sum(1 for r in rows if r["fallback_triggered"]),
            "total_cost_usd": round(sum(r["estimated_cost_usd"] or 0.0 for r in rows), 6),
            "unpriced_calls": sum(1 for r in rows if r["estimated_cost_usd"] is None),
            "by_model": dict(by_model),
        }
