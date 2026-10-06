"""Production-maturity tracking and non-destructive readiness verification."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .architecture import audit_architecture
from .events import append_event
from .reality import source_status

STAGES = (
    "target", "designed", "implemented", "tested", "ci_verified",
    "canary", "soak", "production_verified",
)
TERMINAL_STATES = {"degraded", "revoked"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_maturity(
 component TEXT PRIMARY KEY,
 stage TEXT NOT NULL,
 evidence_ref TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS maturity_checks(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 component TEXT NOT NULL,
 check_kind TEXT NOT NULL,
 passed INTEGER NOT NULL CHECK(passed IN (0,1)),
 evidence_ref TEXT NOT NULL DEFAULT '',
 details_json TEXT NOT NULL DEFAULT '{}',
 occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maturity_checks_lookup
ON maturity_checks(component,check_kind,occurred_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def _ensure(c: sqlite3.Connection) -> None:
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capability_maturity'").fetchone() is None:
        initialize(c)


def sync_foundations(c: sqlite3.Connection) -> dict[str, int]:
    _ensure(c)
    architecture = audit_architecture(c)
    created = 0
    for item in architecture["systems"]:
        if item["status"] != "implemented":
            continue
        exists = c.execute("SELECT 1 FROM capability_maturity WHERE component=?", (item["key"],)).fetchone()
        if exists:
            continue
        c.execute(
            "INSERT INTO capability_maturity(component,stage,evidence_ref,updated_at,metadata_json) VALUES(?,?,?,?,?)",
            (item["key"], "implemented", "architecture-sentinel", _now(), json.dumps({"title": item["title"]}, sort_keys=True)),
        )
        created += 1
    c.commit()
    return {"created": created, "systems": architecture["systems_total"]}


def stage(c: sqlite3.Connection, component: str) -> str | None:
    _ensure(c)
    row = c.execute("SELECT stage FROM capability_maturity WHERE component=?", (component,)).fetchone()
    return None if row is None else str(row["stage"])


def promote(c: sqlite3.Connection, *, component: str, to_stage: str, evidence_ref: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    _ensure(c)
    if to_stage not in STAGES:
        raise ValueError("invalid maturity stage")
    if not evidence_ref.strip():
        raise ValueError("promotion requires evidence_ref")
    current = stage(c, component)
    if current is None:
        if to_stage != "target":
            raise ValueError("unknown component must begin at target")
    elif current in TERMINAL_STATES:
        raise ValueError("degraded or revoked component requires explicit recovery workflow")
    elif STAGES.index(to_stage) != STAGES.index(current) + 1:
        raise ValueError("maturity promotion must advance exactly one stage")
    c.execute(
        """INSERT INTO capability_maturity(component,stage,evidence_ref,updated_at,metadata_json)
        VALUES(?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET stage=excluded.stage,
        evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at,metadata_json=excluded.metadata_json""",
        (component, to_stage, evidence_ref.strip()[:1000], _now(), json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))),
    )
    c.commit()
    append_event(c, "maturity.promoted", {"component": component, "stage": to_stage, "evidence_ref": evidence_ref[:300]})
    return {"component": component, "stage": to_stage}


def mark_state(c: sqlite3.Connection, *, component: str, state_name: str, evidence_ref: str) -> dict[str, Any]:
    _ensure(c)
    if state_name not in TERMINAL_STATES:
        raise ValueError("state_name must be degraded or revoked")
    if stage(c, component) is None:
        raise ValueError("unknown component")
    c.execute("UPDATE capability_maturity SET stage=?,evidence_ref=?,updated_at=? WHERE component=?",
              (state_name, evidence_ref[:1000], _now(), component))
    c.commit()
    append_event(c, "maturity.state_changed", {"component": component, "stage": state_name})
    return {"component": component, "stage": state_name}


def record_check(c: sqlite3.Connection, *, component: str, check_kind: str, passed: bool,
                 evidence_ref: str = "", details: dict[str, Any] | None = None) -> int:
    _ensure(c)
    cur = c.execute(
        "INSERT INTO maturity_checks(component,check_kind,passed,evidence_ref,details_json,occurred_at) VALUES(?,?,?,?,?,?)",
        (component, check_kind[:80], int(passed), evidence_ref[:1000], json.dumps(details or {}, sort_keys=True, separators=(",", ":")), _now()),
    )
    c.commit()
    append_event(c, "maturity.check_recorded", {"component": component, "check_kind": check_kind[:80], "passed": bool(passed)})
    return int(cur.lastrowid)


def connector_readiness(c: sqlite3.Connection) -> dict[str, Any]:
    rows = [x for x in source_status(c) if x["kind"] in {"bridge", "connector"}]
    ready = []
    blocked = []
    for item in rows:
        usable = bool(item["enabled"] and item["health"] == "healthy")
        entry = {"source": item["source_key"], "health": item["health"], "enabled": bool(item["enabled"])}
        (ready if usable else blocked).append(entry)
    return {"total": len(rows), "ready": ready, "not_ready": blocked, "ready_count": len(ready)}


def run_recovery_drill(c: sqlite3.Connection) -> dict[str, Any]:
    """Copy the live SQLite database, open the copy, and run integrity_check only."""
    fd, temp_path = tempfile.mkstemp(prefix="life-os-recovery-", suffix=".db")
    os.close(fd)
    target = sqlite3.connect(temp_path)
    try:
        c.backup(target)
        row = target.execute("PRAGMA integrity_check").fetchone()
        passed = bool(row and row[0] == "ok")
        tables = int(target.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
    finally:
        target.close()
        try:
            Path(temp_path).unlink()
        except OSError:
            pass
    details = {"integrity_check": "ok" if passed else "failed", "tables_seen": tables}
    record_check(c, component="life_os_database", check_kind="recovery_drill", passed=passed,
                 evidence_ref="sqlite-backup-copy-integrity-check", details=details)
    return {"passed": passed, **details}


def record_canary(c: sqlite3.Connection, *, component: str, passed: bool, evidence_ref: str,
                  details: dict[str, Any] | None = None) -> dict[str, Any]:
    if stage(c, component) != "ci_verified":
        raise ValueError("canary requires ci_verified stage")
    record_check(c, component=component, check_kind="canary", passed=passed,
                 evidence_ref=evidence_ref, details=details)
    if not passed:
        mark_state(c, component=component, state_name="degraded", evidence_ref=evidence_ref)
        return {"component": component, "stage": "degraded", "passed": False}
    return {**promote(c, component=component, to_stage="canary", evidence_ref=evidence_ref,
                      metadata=details), "passed": True}


def record_soak(c: sqlite3.Connection, *, component: str, healthy: bool, duration_seconds: int,
                evidence_ref: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    if stage(c, component) != "canary":
        raise ValueError("soak requires canary stage")
    if type(duration_seconds) is not int or duration_seconds < 60:
        raise ValueError("soak must cover at least 60 seconds")
    payload = {"duration_seconds": duration_seconds, **(details or {})}
    record_check(c, component=component, check_kind="soak", passed=healthy,
                 evidence_ref=evidence_ref, details=payload)
    if not healthy:
        mark_state(c, component=component, state_name="degraded", evidence_ref=evidence_ref)
        return {"component": component, "stage": "degraded", "healthy": False}
    return {**promote(c, component=component, to_stage="soak", evidence_ref=evidence_ref,
                      metadata=payload), "healthy": True}


def verify_production(c: sqlite3.Connection, *, component: str, evidence_ref: str,
                      connector_source: str | None = None) -> dict[str, Any]:
    if stage(c, component) != "soak":
        raise ValueError("production verification requires completed soak")
    architecture = audit_architecture(c)
    if architecture["missing"] or architecture["partial"] or architecture["critical_gaps"]:
        raise ValueError("architecture gaps block production verification")
    drill = c.execute("""SELECT passed FROM maturity_checks
        WHERE component='life_os_database' AND check_kind='recovery_drill'
        ORDER BY id DESC LIMIT 1""").fetchone()
    if drill is None or not drill["passed"]:
        raise ValueError("successful recovery drill required")
    if connector_source is not None:
        readiness = connector_readiness(c)
        ready_sources = {x["source"] for x in readiness["ready"]}
        if connector_source not in ready_sources:
            raise ValueError("required connector is not ready")
    record_check(c, component=component, check_kind="production_verification", passed=True,
                 evidence_ref=evidence_ref, details={"connector_source": connector_source})
    return promote(c, component=component, to_stage="production_verified",
                   evidence_ref=evidence_ref, metadata={"connector_source": connector_source})


def readiness_snapshot(c: sqlite3.Connection) -> dict[str, Any]:
    _ensure(c)
    sync_foundations(c)
    architecture = audit_architecture(c)
    connectors = connector_readiness(c)
    rows = [dict(r) for r in c.execute("SELECT component,stage,evidence_ref,updated_at FROM capability_maturity ORDER BY component")]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    latest_drill = c.execute("""SELECT passed,occurred_at FROM maturity_checks
        WHERE component='life_os_database' AND check_kind='recovery_drill' ORDER BY id DESC LIMIT 1""").fetchone()
    result = {
        "architecture": {k: architecture[k] for k in ("systems_total", "implemented", "partial", "missing", "critical_gaps")},
        "maturity_counts": counts,
        "production_verified": counts.get("production_verified", 0),
        "connectors": connectors,
        "latest_recovery_drill": None if latest_drill is None else {"passed": bool(latest_drill["passed"]), "occurred_at": latest_drill["occurred_at"]},
        "components": rows,
    }
    append_event(c, "maturity.readiness_snapshot", {
        "production_verified": result["production_verified"],
        "connector_ready_count": connectors["ready_count"],
        "architecture_gaps": architecture["missing"] + architecture["partial"],
    })
    return result
