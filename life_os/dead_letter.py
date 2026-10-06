"""Deterministic reconciliation for terminal worker jobs.

Dead jobs remain durable history, but terminal queue attempts must not become
permanent operational blockers. Reconciliation hands the obligation to the
correct higher-level owner before moving the stale queue attempt to cancelled.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from .events import append_event
from .queue import dead_jobs, dispose_dead, enqueue, set_state, stats

_TASK_REQUEST = re.compile(r"^Execute LIFE OS task #(\d+):", re.I)
_ACTIVE_JOB_STATES = ("queued", "running", "retry")


def _active_build(connection, run_id: int) -> bool:
    return connection.execute(
        """SELECT 1 FROM worker_jobs WHERE kind='engineering.build'
           AND state IN ('queued','running','retry')
           AND json_extract(payload_json,'$.run_id')=? LIMIT 1""",
        (run_id,),
    ).fetchone() is not None


def _coordinator_successor(connection, key: str, job_id: int) -> dict[str, Any]:
    job, created = enqueue(
        connection,
        fingerprint=f"dead-letter:coordinator:{key}:{job_id}",
        kind="coordinator.scan",
        priority=100,
        max_attempts=3,
    )
    return {"job_id": job.id, "kind": job.kind, "created": created}


def _maintainer_successor(connection, job_id: int) -> dict[str, Any]:
    job, created = enqueue(
        connection,
        fingerprint=f"dead-letter:maintainer:{job_id}",
        kind="maintainer.scan",
        priority=100,
        max_attempts=3,
    )
    return {"job_id": job.id, "kind": job.kind, "created": created}


def _reconcile_request(connection, item: dict[str, Any]) -> str:
    from . import request_fabric
    rid = str(item["payload"].get("request_id", ""))
    row = connection.execute(
        "SELECT id,text,state,error,generation,created_at FROM execution_requests WHERE id=?",
        (rid,),
    ).fetchone()
    if row is None:
        dispose_dead(connection, item["id"], disposition="orphaned_request",
                     detail="execution request no longer exists")
        return "orphaned_request"

    state = str(row["state"])
    error = (str(row["error"]) + " " + str(item["last_error"])).lower()
    if state in {"succeeded", "cancelled"}:
        dispose_dead(connection, item["id"], disposition="request_already_terminal",
                     detail=f"execution request is {state}")
        return "request_already_terminal"
    if "owner paused or cancelled execution" in error:
        request_fabric.supersede(connection, rid, "historical owner-paused validation attempt")
        dispose_dead(connection, item["id"], disposition="owner_cancelled",
                     detail="owner-paused attempt retained only as audit history")
        return "owner_cancelled"

    match = _TASK_REQUEST.match(str(row["text"]))
    if match:
        task_id = match.group(1)
        request_fabric.supersede(
            connection, rid, f"LIFE task #{task_id} remains the canonical obligation owner"
        )
        successor = _coordinator_successor(connection, f"task-{task_id}", item["id"])
        dispose_dead(
            connection, item["id"], disposition="higher_level_task_owner",
            detail=f"LIFE task #{task_id} retained; stale execution attempt retired",
            successor=successor,
        )
        return "higher_level_task_owner"

    if state in {"queued", "planning", "executing", "retry", "waiting_capability"}:
        dispose_dead(connection, item["id"], disposition="active_request_supersedes_attempt",
                     detail=f"execution request remains {state}")
        return "active_request_supersedes_attempt"

    no_action = "no action executed" in error or "no unverified result accepted" in error
    age_seconds = max(0.0, time.time() - float(row["created_at"]))
    if state == "failed" and no_action:
        if age_seconds <= 900 and int(row["generation"]) < 2:
            try:
                from . import ai_cli
                ai_cli.select(connection)
                request_fabric.resume(connection, rid)
                generation = int(row["generation"]) + 1
                dispose_dead(
                    connection, item["id"], disposition="request_resumed_no_side_effect",
                    detail="planner is ready; no side effect occurred before failure",
                    successor={"request_id": rid, "generation": generation},
                )
                return "request_resumed_no_side_effect"
            except ai_cli.CapabilityUnavailable as exc:
                request_fabric.gap(connection, rid, "ai.authentication",
                                   "Dead-letter recovery waiting for planner readiness: " + str(exc))
                dispose_dead(
                    connection, item["id"], disposition="waiting_capability",
                    detail="request moved to readiness-driven capability wait",
                    successor={"request_id": rid, "state": "waiting_capability"},
                )
                return "waiting_capability"
        request_fabric.supersede(
            connection, rid, "stale no-side-effect request replaced by current control-plane state"
        )
        successor = _maintainer_successor(connection, item["id"])
        dispose_dead(
            connection, item["id"], disposition="stale_request_superseded",
            detail="original request and failure remain in execution history",
            successor=successor,
        )
        return "stale_request_superseded"

    successor = _maintainer_successor(connection, item["id"])
    dispose_dead(
        connection, item["id"], disposition="quarantined_with_repair_owner",
        detail="unrecognized terminal request class retained for maintainer reconciliation",
        successor=successor,
    )
    return "quarantined_with_repair_owner"


def _reconcile_engineering(connection, item: dict[str, Any]) -> str:
    run_id = int(item["payload"].get("run_id", 0) or 0)
    row = connection.execute(
        "SELECT id,state,run_key FROM engineering_runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None:
        dispose_dead(connection, item["id"], disposition="orphaned_engineering_attempt",
                     detail="engineering run no longer exists")
        return "orphaned_engineering_attempt"
    state = str(row["state"])
    successor: dict[str, Any] = {"run_id": run_id, "state": state}
    if state in {"building", "verifying"} and not _active_build(connection, run_id):
        from .engineering import _set_state, schedule_waiting
        _set_state(
            connection, run_id, "waiting_provider", provider="",
            failure_reason="Recovered stale engineering state after terminal/interrupted queue attempt",
        )
        created = schedule_waiting(connection, interval_seconds=60)
        successor.update({"state": "waiting_provider", "jobs_created": created})
        append_event(connection, "engineering.run.dead_letter_recovered", {"run_id": run_id})
        disposition = "stale_engineering_state_recovered"
    elif state == "waiting_provider":
        from .engineering import schedule_waiting
        successor["jobs_created"] = schedule_waiting(connection, interval_seconds=60)
        disposition = "engineering_wait_owner_retained"
    else:
        disposition = "engineering_run_owns_terminal_attempt"
    dispose_dead(
        connection, item["id"], disposition=disposition,
        detail=f"engineering run #{run_id} remains authoritative in state {successor['state']}",
        successor=successor,
    )
    return disposition


def _reconcile_scheduled(connection, item: dict[str, Any]) -> str:
    task_id = str(item["payload"].get("task_id", ""))
    row = connection.execute(
        "SELECT id,directive,state,next_run_at FROM scheduled_tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or row["state"] != "active":
        dispose_dead(connection, item["id"], disposition="inactive_schedule_attempt",
                     detail="schedule is missing or inactive")
        return "inactive_schedule_attempt"
    if not str(row["directive"]).strip():
        connection.execute(
            "UPDATE scheduled_tasks SET state='paused',last_error=?,updated_at=? WHERE id=?",
            ("Paused by dead-letter reconciler: empty directive",
             datetime.now(timezone.utc).isoformat(), task_id),
        )
        connection.commit()
        dispose_dead(connection, item["id"], disposition="invalid_schedule_paused",
                     detail="empty directive paused to prevent recurrence")
        return "invalid_schedule_paused"
    successor: dict[str, Any] = {"task_id": task_id, "next_run_at": row["next_run_at"]}
    if str(row["next_run_at"]) <= datetime.now(timezone.utc).isoformat():
        job, created = enqueue(
            connection,
            fingerprint=f"command-center:scheduled-reconcile:{task_id}:{item['id']}",
            kind="command_center.scheduled",
            payload={"task_id": task_id, "scheduled_for": row["next_run_at"]},
            priority=94,
            max_attempts=3,
        )
        successor.update({"job_id": job.id, "created": created})
    dispose_dead(connection, item["id"], disposition="valid_schedule_supersedes_attempt",
                 detail="current active schedule owns future execution", successor=successor)
    return "valid_schedule_supersedes_attempt"


def reconcile(connection, *, limit: int = 100) -> dict[str, Any]:
    items = dead_jobs(connection, limit=limit)
    dispositions: dict[str, int] = {}
    processed = 0
    for item in items:
        if item["kind"] == "request.execute":
            disposition = _reconcile_request(connection, item)
        elif item["kind"] == "engineering.build":
            disposition = _reconcile_engineering(connection, item)
        elif item["kind"] == "command_center.scheduled":
            disposition = _reconcile_scheduled(connection, item)
        else:
            successor = _maintainer_successor(connection, item["id"])
            dispose_dead(
                connection, item["id"], disposition="quarantined_with_repair_owner",
                detail=f"unrecognized terminal job kind: {item['kind']}", successor=successor,
            )
            disposition = "quarantined_with_repair_owner"
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
        processed += 1
    remaining = stats(connection)["dead"]
    result = {
        "processed": processed, "remaining_dead": remaining,
        "dispositions": dispositions, "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    set_state(connection, "dead_letter.last_reconcile", json.dumps(result, sort_keys=True))
    append_event(connection, "dead_letter.reconciled", result)
    return result
