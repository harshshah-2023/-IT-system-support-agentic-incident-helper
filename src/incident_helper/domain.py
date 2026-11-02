"""Domain vocabulary shared by every layer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"


class Incident(BaseModel):
    id: str
    title: str
    severity: Severity
    status: IncidentStatus = IncidentStatus.OPEN
    service: str | None = None
    summary: str = ""
    sentiment: str = ""
    first_steps: list[str] = Field(default_factory=list)
    source_message_id: str | None = None
    opened_at: str = ""


class ServiceStatus(BaseModel):
    service: str
    state: str
    version: str
    tier: int
    owner: str
    p95_ms: int
    error_rate_pct: float
    healthy_instances: int
    instances: int
    dependencies: list[str]
    degraded_dependencies: list[str]
    last_deployment_id: str | None = None


class DeploymentRecord(BaseModel):
    id: str
    service: str
    version: str
    previous_version: str | None
    deployed_at: str
    author: str
    status: str
    change_summary: str
    config_diff: dict[str, str]
    ticket: str | None = None
