"""Internal autonomous roles executed by the persistent LIFE OS worker."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import audit
from . import autonomy_maintenance
from . import architecture_gap
from . import performance
from . import reality
from . import revenue_followthrough
from .backup import create_backup
from .ai_cli import run_bounded
from .events import append_event
from .queue import Job, enqueue, set_state, stats
from .purchases import active_milestone
from .service import LifeOS

MAINTENANCE_TIMEOUT_SECONDS = 600


def _maintenance_child(connection, kind, *, pulse, backups=None):
    import sys
    database = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not database:
        raise ValueError("bounded maintenance requires a durable SQLite database")
    argv = [sys.executable, "-m", "life_os.maintenance_child", kind, "--db", database]
    if backups is not None:
        argv += ["--backup-dir", str(backups)]
    code, out, _err = run_bounded(argv, cwd=Path(__file__).resolve().parents[1],
                                 timeout=MAINTENANCE_TIMEOUT_SECONDS, pulse=pulse)
    if code:
        raise RuntimeError(f"bounded {kind} failed (exit {code})")
    result = json.loads(out)
    if not isinstance(result, dict):
        raise ValueError("maintenance child returned invalid result")
    return result


def coordinator_scan(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    generated = 0
    specs = (
        ("coordinator.plan_snapshot", 90),
        ("coordinator.commitment_sweep", 80),
        ("coordinator.purchase_sweep", 75),
    )
    for kind, priority in specs:
        _, created = enqueue(
            connection,
            fingerprint=f"{job.fingerprint}:{kind}",
            kind=kind,
            priority=priority,
        )
        generated += int(created)
    return {"generated": generated}


def coordinator_plan_snapshot(
    connection: sqlite3.Connection, job: Job
) -> dict[str, Any]:
    os = LifeOS(connection)
    plan = os.today()
    next_item = plan[0] if plan else None
    result = {
        "date": date.today().isoformat(),
        "candidate_count": len(os.candidates()),
        "planned_count": len(plan),
        "next_task_id": None if next_item is None else next_item.task.id,
        "next_score": None if next_item is None else next_item.score,
    }
    append_event(connection, "worker.coordinator.plan_verified", result)
    set_state(connection, "coordinator.last_plan", json.dumps(result, sort_keys=True))
    return result


def coordinator_commitment_sweep(
    connection: sqlite3.Connection, job: Job
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    rows = connection.execute(
        """
        SELECT id,starts_at,status FROM commitments
        WHERE status='scheduled'
        ORDER BY starts_at,id
        """
    ).fetchall()
    overdue = sum(1 for row in rows if row["starts_at"] < now)
    result = {"scheduled": len(rows), "past_start_unresolved": overdue}
    append_event(connection, "worker.coordinator.commitments_verified", result)
    return result


def coordinator_purchase_sweep(
    connection: sqlite3.Connection, job: Job
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT status,COUNT(*) AS n FROM purchases GROUP BY status"
    ).fetchall()
    by_status = {row["status"]: row["n"] for row in rows}
    bucket_rows = connection.execute(
        """SELECT pp.bucket,COUNT(*) AS n FROM purchase_policy pp
           JOIN purchases p ON p.id=pp.purchase_id WHERE p.status='wanted'
           GROUP BY pp.bucket"""
    ).fetchall()
    milestone = active_milestone(connection)
    result = {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_bucket": {row["bucket"]: row["n"] for row in bucket_rows},
        "active_milestone": None if milestone is None else dict(milestone),
    }
    append_event(connection, "worker.coordinator.purchases_verified", result)
    set_state(connection, "coordinator.procurement", json.dumps(result, sort_keys=True))
    return result


def maintainer_scan(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    generated = 0
    specs = (
        ("maintainer.audit", 100),
        ("maintainer.autonomy_health", 95),
        ("maintainer.architecture_gap", 92),
        ("maintainer.production_readiness", 88),
        ("maintainer.backup", 85),
        ("maintainer.queue_health", 80),
        ("maintainer.capability_probe", 70),
        ("maintainer.communication_evidence", 65),
    )
    for kind, priority in specs:
        _, created = enqueue(
            connection,
            fingerprint=f"{job.fingerprint}:{kind}",
            kind=kind,
            priority=priority,
        )
        generated += int(created)
    return {"generated": generated}


def maintainer_audit(connection: sqlite3.Connection, job: Job, *, pulse=None) -> dict[str, Any]:
    problems = audit.run(connection) if pulse is None else _maintenance_child(connection, "audit", pulse=pulse).get("problems")
    if not isinstance(problems, list) or any(not isinstance(item, str) for item in problems):
        raise ValueError("maintenance audit returned invalid problems")
    result = {"healthy": not problems, "problems": problems}
    set_state(connection, "maintainer.last_audit", json.dumps(result, sort_keys=True))
    append_event(connection, "worker.maintainer.audit_verified", result)
    return result


def maintainer_backup(
    connection: sqlite3.Connection, job: Job, backups: Path, *, pulse=None
) -> dict[str, Any]:
    backups.mkdir(parents=True, exist_ok=True)
    today_prefix = f"life-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    existing = sorted(backups.glob(f"{today_prefix}*.db"))
    if existing:
        result = {"created": False, "path": str(existing[-1])}
    elif pulse is not None:
        result = _maintenance_child(connection, "backup", pulse=pulse, backups=backups)
        if result.get("created") is not True or not isinstance(result.get("path"), str):
            raise ValueError("maintenance backup returned invalid receipt")
        path = Path(result["path"]).resolve()
        if not path.is_relative_to(backups.resolve()) or not path.is_file():
            raise ValueError("maintenance backup returned invalid artifact")
    else:
        path = create_backup(connection, backups)
        result = {"created": True, "path": str(path)}
    set_state(connection, "maintainer.last_backup", json.dumps(result, sort_keys=True))
    append_event(connection, "worker.maintainer.backup_verified", result)
    return result


def maintainer_queue_health(
    connection: sqlite3.Connection, job: Job
) -> dict[str, Any]:
    result = stats(connection)
    set_state(connection, "maintainer.queue_stats", json.dumps(result, sort_keys=True))
    return result


def maintainer_architecture_gap(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    """Preserve the existing proposal scanner and also audit top-level coverage."""
    result = architecture_gap.scan(connection)
    from .architecture import audit_architecture

    top_level = audit_architecture(connection)
    result["top_level_systems"] = top_level["systems_total"]
    result["top_level_missing"] = top_level["missing"]
    result["top_level_partial"] = top_level["partial"]
    result["top_level_critical_gaps"] = top_level["critical_gaps"]
    return result


def maintainer_capability_probe(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    return autonomy_maintenance.capability_probe(connection)


def maintainer_autonomy_health(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    return autonomy_maintenance.autonomy_health(connection)


def maintainer_communication_evidence(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    from .communication_engine import evidence_review
    from .research import request_research

    review = evidence_review(connection)
    requested = 0
    for item in review:
        if item["sources"] >= 2:
            continue
        _, created = request_research(
            connection,
            query=f"independent research evidence for {item['technique']} communication persuasion technique systematic review meta analysis",
            purpose=f"communication_evidence:{item['technique']}",
            freshness_seconds=2_592_000,
            max_sources=6,
        )
        requested += int(created)
    return {"techniques": len(review), "research_requested": requested}


def maintainer_production_readiness(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    from .maturity import readiness_snapshot

    result = readiness_snapshot(connection)
    set_state(connection, "maintainer.production_readiness", json.dumps(result, sort_keys=True))
    return {
        "production_verified": result["production_verified"],
        "connector_ready_count": result["connectors"]["ready_count"],
        "architecture_missing": result["architecture"]["missing"],
        "architecture_partial": result["architecture"]["partial"],
    }


def maturity_recovery_drill(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    from .maturity import run_recovery_drill

    return run_recovery_drill(connection)


def performance_scan(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    result = performance.run()
    set_state(connection, "performance.last_scan", json.dumps(result, sort_keys=True))
    append_event(connection, "worker.performance.scan_verified", result)
    return result


def reality_scan(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    result = reality.run_reality_scan(connection)
    from .control_bridge_client import poll_once

    bridge = poll_once(connection)
    if isinstance(result, dict):
        result["control_bridge"] = bridge
    set_state(connection, "reality.last_scan", json.dumps(result, sort_keys=True))
    return result


def revenue_followthrough_scan(connection: sqlite3.Connection, job: Job) -> dict[str, Any]:
    return revenue_followthrough.reconcile(connection)
