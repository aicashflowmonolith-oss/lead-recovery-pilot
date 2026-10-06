"""Deterministic architecture-gap sentinel and proposal ledger."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .events import append_event
from .queue import set_state

TARGET_LEDGER_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS architecture_gap_proposals(
    gap_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    proposal_kind TEXT NOT NULL DEFAULT 'code' CHECK(proposal_kind IN ('code','external','human')),
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved','suppressed')),
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


@dataclass(frozen=True)
class ArchitectureTarget:
    key: str
    title: str
    priority: int
    required_modules: tuple[str, ...]
    rationale: str
    proposal_kind: str = "code"


TARGETS: tuple[ArchitectureTarget, ...] = (
    ArchitectureTarget("execution.machine_operator", "Native browser and Windows machine operator", 100,
                       ("browser_operator.py", "windows_operator.py"),
                       "Native observe-plan-act-verify browser and Windows UI execution remains absent."),
    ArchitectureTarget("recovery.safe_mode", "Independent safe-mode kernel", 99,
                       ("safe_mode.py",),
                       "A minimal diagnostics, pause, backup/restore and recovery kernel must survive planner failure."),
    ArchitectureTarget("security.sentinel", "Prompt-injection and security sentinel", 98,
                       ("security_sentinel.py",),
                       "Untrusted-content boundaries require a dedicated deterministic security sentinel."),
    ArchitectureTarget("observability.tracing", "Structured tracing and trajectory observability", 95,
                       ("observability.py",),
                       "Agent trajectories, tool provenance and controller decisions need structured tracing."),
    ArchitectureTarget("evaluation.regression", "Deterministic evaluation and regression system", 94,
                       ("evaluation.py",),
                       "Golden tasks, security regressions and recovery drills need a first-class evaluation controller."),
    ArchitectureTarget("skills.demonstration_learning", "Verified demonstration-to-skill pipeline", 90,
                       ("skill_learning.py",),
                       "Demonstrated workflows need sandbox replay, provenance, versioning and verification before use."),
    ArchitectureTarget("memory.second_brain", "Tool-agnostic second-brain memory layer", 90,
                       ("memory.py",),
                       "Structured episodic, semantic and procedural memory needs an owned retrieval layer."),
    ArchitectureTarget("engineering.self_upgrade", "Governed engineering self-upgrade pipeline", 100,
                       ("engineering.py", "engineering_delivery.py"),
                       "Engineering changes require isolated builds, verification, CI delivery and bounded promotion."),
    ArchitectureTarget("engineering.provider_routing", "Multi-provider engineering routing", 95,
                       ("capabilities.py", "ai_cli.py"),
                       "Engineering must route through replaceable providers rather than a single model."),
    ArchitectureTarget("execution.request_fabric", "Governed external request fabric", 95,
                       ("request_fabric.py", "execution.py"),
                       "External execution requires durable, policy-bounded request and verification primitives."),
    ArchitectureTarget("recovery.verified", "Verified backup, audit and fallback recovery", 95,
                       ("backup.py", "audit.py", "fallback.py"),
                       "Recovery needs verified backups, integrity audit and bounded fallback handling."),
    ArchitectureTarget("money.authoritative_evidence", "Authoritative payment evidence boundary", 95,
                       ("money.py", "revenue_engine.py"),
                       "Revenue truth must remain tied to authoritative settled payment evidence."),
    ArchitectureTarget("human.executor_broker", "Governed human executor broker", 80,
                       ("human_exec.py",),
                       "Human service comparison and consequential hiring gates need explicit governance."),
    ArchitectureTarget("sentinel.architecture_gap", "Architecture-gap sentinel", 100,
                       ("architecture_gap.py",),
                       "The maintained architecture target must continuously produce bounded proposals for missing code."),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def _evidence(root: Path, target: ArchitectureTarget) -> dict[str, Any]:
    present = [name for name in target.required_modules if (root / name).is_file()]
    missing = [name for name in target.required_modules if name not in present]
    return {"required_modules": list(target.required_modules), "present": present, "missing": missing}


def scan(
    connection: sqlite3.Connection,
    *,
    module_root: Path | None = None,
    targets: Sequence[ArchitectureTarget] = TARGETS,
    max_targets: int = 64,
) -> dict[str, Any]:
    initialize(connection)
    if len(targets) > max_targets:
        raise ValueError("architecture target scan exceeds bounded target limit")
    root = (module_root or Path(__file__).resolve().parent).resolve()
    now = _now()
    created = refreshed = resolved = suppressed = 0
    open_keys: list[str] = []
    for target in sorted(targets, key=lambda item: (-item.priority, item.key)):
        evidence = _evidence(root, target)
        existing = connection.execute(
            "SELECT state FROM architecture_gap_proposals WHERE gap_key=?", (target.key,)
        ).fetchone()
        if not evidence["missing"]:
            if existing is not None and existing["state"] == "open":
                connection.execute(
                    "UPDATE architecture_gap_proposals SET state='resolved',last_seen_at=?,resolved_at=?,evidence_json=? WHERE gap_key=?",
                    (now, now, json.dumps(evidence, sort_keys=True), target.key),
                )
                resolved += 1
            continue
        if existing is not None and existing["state"] == "suppressed":
            connection.execute(
                "UPDATE architecture_gap_proposals SET last_seen_at=?,evidence_json=? WHERE gap_key=?",
                (now, json.dumps(evidence, sort_keys=True), target.key),
            )
            suppressed += 1
            continue
        open_keys.append(target.key)
        evidence_json = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
        if existing is None:
            connection.execute(
                """INSERT INTO architecture_gap_proposals(
                gap_key,title,priority,proposal_kind,state,rationale,evidence_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?, 'open',?,?,?,?)""",
                (target.key, target.title, target.priority, target.proposal_kind,
                 target.rationale, evidence_json, now, now),
            )
            created += 1
        else:
            connection.execute(
                """UPDATE architecture_gap_proposals
                SET title=?,priority=?,proposal_kind=?,state='open',rationale=?,evidence_json=?,
                    last_seen_at=?,resolved_at=NULL WHERE gap_key=?""",
                (target.title, target.priority, target.proposal_kind, target.rationale,
                 evidence_json, now, target.key),
            )
            refreshed += 1
    connection.commit()
    result = {
        "ledger_version": TARGET_LEDGER_VERSION,
        "targets_scanned": len(targets),
        "open": len(open_keys),
        "created": created,
        "refreshed": refreshed,
        "resolved": resolved,
        "suppressed": suppressed,
        "open_keys": open_keys[:max_targets],
    }
    set_state(connection, "architecture_gap.last_scan", json.dumps(result, sort_keys=True))
    append_event(connection, "architecture.gap_scanned", result)
    return result


def recent_proposals(connection: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    initialize(connection)
    rows = connection.execute(
        "SELECT * FROM architecture_gap_proposals ORDER BY state='open' DESC,priority DESC,last_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    values = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        values.append(item)
    return values


def summary(connection: sqlite3.Connection) -> dict[str, int]:
    initialize(connection)
    counts = {row["state"]: int(row["n"]) for row in connection.execute(
        "SELECT state,COUNT(*) AS n FROM architecture_gap_proposals GROUP BY state"
    )}
    for state in ("open", "resolved", "suppressed"):
        counts.setdefault(state, 0)
    return counts
