"""Simulated production estate backed by SQLite.

Every tool reads or mutates this store, so answers are derived from state rather than scripted.
A service's health comes from the profile of the version it currently runs, which means a rollback
changes what `check_service_status` reports afterwards. No real infrastructure is ever touched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_helper.config import ROOT
from incident_helper.domain import DeploymentRecord, Incident, IncidentStatus, ServiceStatus

WORLD_PATH = ROOT / "data" / "fixtures" / "ops_world.json"
INBOX_PATH = ROOT / "data" / "fixtures" / "inbox.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, data TEXT NOT NULL, version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY, service TEXT NOT NULL, deployed_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class OpsError(ValueError):
    """A tool was asked about something that does not exist."""


class RollbackRefused(OpsError):
    """Deterministic policy refused a rollback, regardless of what any model or human asked for."""


class OpsStore:
    def __init__(self, path: Path, world_path: Path = WORLD_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._world_path = world_path
        self._world: dict[str, Any] = json.loads(world_path.read_text(encoding="utf-8"))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        if not self._conn.execute("SELECT 1 FROM services LIMIT 1").fetchone():
            self.reset()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    def reset(self) -> None:
        """Restore the estate to the fixture snapshot (the moment the incident night was recorded)."""
        with self._tx() as db:
            for table in ("services", "deployments", "incidents", "audit", "kv"):
                db.execute(f"DELETE FROM {table}")
            for svc in self._world["services"]:
                db.execute(
                    "INSERT INTO services VALUES (?, ?, ?)",
                    (svc["name"], json.dumps(svc), self._world["current_versions"][svc["name"]]),
                )
            for dep in self._world["deployments"]:
                db.execute(
                    "INSERT INTO deployments VALUES (?, ?, ?, ?)",
                    (dep["id"], dep["service"], dep["deployed_at"], json.dumps(dep)),
                )

    # ------------------------------------------------------------------ read side
    def service_names(self) -> list[str]:
        return [row["name"] for row in self._conn.execute("SELECT name FROM services ORDER BY name")]

    def _service_row(self, service: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM services WHERE name = ?", (service,)).fetchone()
        if row is None:
            raise OpsError(f"Unknown service {service!r}. Known services: {', '.join(self.service_names())}")
        return row

    def _profile(self, service: str, version: str) -> dict[str, Any]:
        profile = self._world["version_profiles"].get(service, {}).get(version)
        if profile is None:
            return {"state": "unknown", "p95_ms": 0, "error_rate_pct": 0.0}
        return profile

    def service_status(self, service: str) -> ServiceStatus:
        row = self._service_row(service)
        meta = json.loads(row["data"])
        version = row["version"]
        profile = self._profile(service, version)
        degraded = []
        for dep in meta["dependencies"]:
            dep_row = self._service_row(dep)
            if self._profile(dep, dep_row["version"])["state"] != "healthy":
                degraded.append(dep)
        latest = self._conn.execute(
            "SELECT id FROM deployments WHERE service = ? ORDER BY deployed_at DESC LIMIT 1", (service,)
        ).fetchone()
        return ServiceStatus(
            service=service,
            state=profile["state"],
            version=version,
            tier=meta["tier"],
            owner=meta["owner"],
            p95_ms=profile["p95_ms"],
            error_rate_pct=profile["error_rate_pct"],
            healthy_instances=meta["healthy_instances"],
            instances=meta["instances"],
            dependencies=meta["dependencies"],
            degraded_dependencies=degraded,
            last_deployment_id=latest["id"] if latest else None,
        )

    def recent_deployments(self, service: str, limit: int = 5) -> list[DeploymentRecord]:
        self._service_row(service)
        rows = self._conn.execute(
            "SELECT data FROM deployments WHERE service = ? ORDER BY deployed_at DESC LIMIT ?",
            (service, limit),
        ).fetchall()
        return [DeploymentRecord.model_validate(json.loads(row["data"])) for row in rows]

    def deployment(self, deployment_id: str) -> DeploymentRecord:
        row = self._conn.execute("SELECT data FROM deployments WHERE id = ?", (deployment_id,)).fetchone()
        if row is None:
            raise OpsError(f"Unknown deployment {deployment_id!r}")
        return DeploymentRecord.model_validate(json.loads(row["data"]))

    def metrics(self, service: str) -> dict[str, list[list[Any]]]:
        self._service_row(service)
        return self._world["metrics"].get(service, {})

    def logs(self, service: str, level: str | None = None) -> list[dict[str, str]]:
        self._service_row(service)
        lines = self._world["logs"].get(service, [])
        return [line for line in lines if level is None or line["level"] == level.upper()]

    def incident_history(self, service: str | None = None) -> list[dict[str, str]]:
        history = self._world["incident_history"]
        return [item for item in history if service is None or item["service"] == service]

    def timeline(self, incident_id: str) -> list[dict[str, str]]:
        events = self._world["timelines"].get(incident_id)
        if events is None:
            raise OpsError(f"No timeline recorded for {incident_id!r}")
        audit = [
            {"ts": row["at"], "event": row["action"] + ": " + row["detail"], "source": "audit-log"}
            for row in self._conn.execute("SELECT * FROM audit ORDER BY seq")
            if incident_id in row["detail"]
        ]
        return [*events, *audit]

    def inbox(self) -> list[dict[str, str]]:
        return json.loads(INBOX_PATH.read_text(encoding="utf-8"))

    def estate_overview(self) -> list[ServiceStatus]:
        return [self.service_status(name) for name in self.service_names()]

    # ------------------------------------------------------------------ write side
    def validate_rollback(self, service: str, version: str) -> str:
        """Return the current version, or raise `RollbackRefused`. Pure check: changes nothing."""
        try:
            current = self._service_row(service)["version"]
        except OpsError as exc:
            raise RollbackRefused(str(exc)) from exc
        records = self.recent_deployments(service, limit=50)
        rollback_targets = {r.version for r in records if r.status == "completed"}
        rollback_targets |= {r.previous_version for r in records if r.previous_version}
        if version == current:
            raise RollbackRefused(f"{service} already runs {version}")
        if version not in rollback_targets:
            raise RollbackRefused(
                f"{version} is not a known good release of {service}. "
                f"Candidates: {', '.join(sorted(rollback_targets - {current})) or 'none'}"
            )
        return current

    def rollback(self, service: str, version: str, approved_by: str, incident_id: str = "") -> dict[str, Any]:
        """Apply a validated rollback and write the audit record atomically."""
        current = self.validate_rollback(service, version)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._tx() as db:
            count = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            deployment = DeploymentRecord(
                id=f"rbk-{count + 1:04d}",
                service=service,
                version=version,
                previous_version=current,
                deployed_at=now,
                author=approved_by,
                status="rollback",
                change_summary=f"Rollback from {current} approved by {approved_by}.",
                config_diff={},
                ticket=incident_id or None,
            )
            db.execute("UPDATE services SET version = ? WHERE name = ?", (version, service))
            meta = json.loads(self._service_row(service)["data"])
            meta["healthy_instances"] = meta["instances"]
            db.execute("UPDATE services SET data = ? WHERE name = ?", (json.dumps(meta), service))
            db.execute(
                "INSERT INTO deployments VALUES (?, ?, ?, ?)",
                (deployment.id, service, now, deployment.model_dump_json()),
            )
            db.execute(
                "INSERT INTO audit (at, action, detail) VALUES (?, ?, ?)",
                (
                    now,
                    "rollback_executed",
                    f"{incident_id} {service} {current} -> {version} by {approved_by}",
                ),
            )
        return {"deployment_id": deployment.id, "from": current, "to": version}

    def audit(self, action: str, detail: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._tx() as db:
            db.execute("INSERT INTO audit (at, action, detail) VALUES (?, ?, ?)", (now, action, detail))

    def audit_log(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM audit ORDER BY seq DESC")]

    # ------------------------------------------------------------------ incidents
    def save_incident(self, incident: Incident) -> Incident:
        with self._tx() as db:
            db.execute(
                "INSERT INTO incidents VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (incident.id, incident.model_dump_json()),
            )
        return incident

    def get_incident(self, incident_id: str) -> Incident:
        row = self._conn.execute("SELECT data FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            raise OpsError(f"Unknown incident {incident_id!r}")
        return Incident.model_validate_json(row["data"])

    def list_incidents(self) -> list[Incident]:
        rows = self._conn.execute("SELECT data FROM incidents ORDER BY id").fetchall()
        return [Incident.model_validate_json(row["data"]) for row in rows]

    def set_incident_status(self, incident_id: str, status: IncidentStatus) -> Incident:
        incident = self.get_incident(incident_id)
        return self.save_incident(incident.model_copy(update={"status": status}))
