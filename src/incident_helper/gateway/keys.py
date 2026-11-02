"""API-key pool: round-robin load balancing with cool-down for keys that fail.

Keys are identified in logs and telemetry only by a short SHA-256 fingerprint.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8] if key else "no-key"


class KeyPool:
    def __init__(
        self,
        keys: list[str],
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keys = keys or [""]
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._blocked_until: dict[str, float] = {}
        self._cursor = 0
        self._lock = threading.Lock()

    @classmethod
    def parse(cls, raw: str, **kwargs: object) -> KeyPool:
        return cls([k.strip() for k in raw.split(",") if k.strip()], **kwargs)  # type: ignore[arg-type]

    def ordered(self) -> list[str]:
        """Keys to try for one request: rotated start, cooled-down keys last rather than dropped."""
        with self._lock:
            start = self._cursor % len(self._keys)
            self._cursor += 1
            rotated = self._keys[start:] + self._keys[:start]
            now = self._clock()
            ready = [k for k in rotated if self._blocked_until.get(k, 0.0) <= now]
            cooling = [k for k in rotated if k not in ready]
            return ready + cooling

    def report_failure(self, key_id: str) -> None:
        with self._lock:
            for key in self._keys:
                if fingerprint(key) == key_id:
                    self._blocked_until[key] = self._clock() + self._cooldown
