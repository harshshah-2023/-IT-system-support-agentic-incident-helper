"""PII and credential sanitisation with per-type REDACT / MASK / HASH / BLOCK policies."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class PiiAction(StrEnum):
    REDACT = "redact"
    MASK = "mask"
    HASH = "hash"
    BLOCK = "block"


def _luhn(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


# Order matters: earlier detectors consume text, so credentials are matched before generic numbers.
DETECTORS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool] | None], ...] = (
    (
        "API_KEY",
        re.compile(
            r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_\-]{6,}\b"
            r"|\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{20,}\b|\bgh[ousr]_[A-Za-z0-9]{20,}\b"
            r"|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
        ),
        None,
    ),
    (
        "PASSWORD",
        re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token)\b\s*[=:]\s*[\"']?[^\s\"',;]+"),
        None,
    ),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), None),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b"), _luhn),
    (
        "IP_ADDRESS",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        None,
    ),
    (
        "PHONE",
        re.compile(
            r"(?<![\w.])(?<!\d[ -])(?:\+\d{1,3}[ -]?)?(?:\(\d{2,4}\)[ -]?)?\d{3,5}[ -]\d{3,5}(?:[ -]\d{2,5})?"
            r"(?![ -]?\d)(?![\w.])|(?<![\w.])\+\d{10,14}(?!\w)|(?<![\w.])[6-9]\d{9}(?!\w)"
        ),
        None,
    ),
)

DEFAULT_POLICY: Mapping[str, PiiAction] = {
    "EMAIL": PiiAction.REDACT,
    "PHONE": PiiAction.REDACT,
    "CREDIT_CARD": PiiAction.REDACT,
    "API_KEY": PiiAction.REDACT,
    "PASSWORD": PiiAction.REDACT,
    "IP_ADDRESS": PiiAction.REDACT,
}


@dataclass(slots=True)
class PiiFinding:
    kind: str
    action: PiiAction


@dataclass(slots=True)
class SanitizedText:
    text: str
    findings: list[PiiFinding] = field(default_factory=list)
    blocked: bool = False

    @property
    def kinds(self) -> list[str]:
        return sorted({f.kind for f in self.findings})


def _apply(kind: str, value: str, action: PiiAction) -> str:
    if action is PiiAction.MASK:
        tail = value[-4:] if kind in {"CREDIT_CARD", "PHONE"} else value[:1]
        return f"{'*' * max(len(value) - 4, 3)}{tail}" if kind in {"CREDIT_CARD", "PHONE"} else f"{tail}***"
    if action is PiiAction.HASH:
        return f"[{kind}_{hashlib.sha256(value.encode()).hexdigest()[:10]}]"
    return f"[REDACTED_{kind}]"


def sanitize(text: str, policy: Mapping[str, PiiAction] = DEFAULT_POLICY) -> SanitizedText:
    """Apply the policy to each detected value. BLOCK still redacts, so that the blocked text is safe to log."""
    result = SanitizedText(text=text)
    for kind, pattern, validator in DETECTORS:
        action = policy.get(kind)
        if action is None:
            continue

        def replace(
            match: re.Match[str], kind: str = kind, action: PiiAction = action, validator=validator
        ) -> str:
            value = match[0]
            if validator is not None and not validator(value):
                return value
            result.findings.append(PiiFinding(kind, action))
            if action is PiiAction.BLOCK:
                result.blocked = True
                return _apply(kind, value, PiiAction.REDACT)
            return _apply(kind, value, action)

        result.text = pattern.sub(replace, result.text)
    return result
