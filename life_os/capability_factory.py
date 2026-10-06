"""Provider-free dispatch into fixed local recipes or governed engineering.

An engineering commit or a generated draft is not an installed capability.
Unknown capabilities keep their original request and wait for normal qualified
promotion/activation; this module cannot execute candidate text or grant gates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from . import engineering, local_capability_recipes as recipes
from .ai_cli import AdapterError, CapabilityUnavailable
from .events import append_event
from .queue import enqueue

ACCEPTANCE = [
    "Prepare only an isolated engineering branch and meaningful tests. Preserve authority gates for spending, "
    "credentials, identity/KYC, contracts, permissions, external communications, destructive actions and production deployment.",
    "Implement a fixed deterministic local recipe or an explicitly allowlisted handler. A model answer draft "
    "does not qualify, install, activate or authorize the missing capability.",
    "Test unavailable model providers and resumption of the original request without replaying any completed "
    "operation or losing original request identifiers and persisted receipts; converge duplicate gaps onto one run.",
    "Require normal exact-head tests/CI and governed promotion/activation before the handler becomes eligible. "
    "A verified engineering commit alone must leave the parent capability gap unresolved.",
]


def _save(c, row, state, receipt):
    c.execute("UPDATE capability_build_tasks SET state=?,recipe_receipt_json=?,updated_at=? WHERE id=?",
              (state, json.dumps(receipt, sort_keys=True), time.time(), row["id"]))
    c.commit()


def _home(c, home):
    if home is not None:
        return Path(home).resolve()
    database = next((r[2] for r in c.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not database:
        raise ValueError("Capability recovery requires a durable local database")
    return Path(database).resolve().parent


def _objective(row):
    reason = " ".join(row["reason"].split())
    identity = hashlib.sha256(json.dumps({"capability": row["capability"], "reason": reason},
                                        sort_keys=True).encode()).hexdigest()
    return "Implement missing capability " + identity + ": " + reason


def enqueue_gap(c, gap_id):
    """One active factory job per gap; later faults retain the original ledger."""
    active = c.execute("SELECT 1 FROM worker_jobs WHERE kind='capability.build' AND state IN ('queued','running','retry') "
                       "AND json_extract(payload_json,'$.gap_id')=? LIMIT 1", (gap_id,)).fetchone()
    if active:
        return False
    original = c.execute("SELECT 1 FROM worker_jobs WHERE fingerprint=?", ("capability.build:" + gap_id,)).fetchone()
    if original:
        row = c.execute("SELECT recovery_generation FROM capability_build_tasks WHERE id=?", (gap_id,)).fetchone()
        generation = row[0] + 1
        c.execute("UPDATE capability_build_tasks SET recovery_generation=? WHERE id=?", (generation, gap_id))
        fingerprint = f"capability.build:{gap_id}:recovery:{generation}"
    else:
        fingerprint = "capability.build:" + gap_id
    _job, inserted = enqueue(c, fingerprint=fingerprint, kind="capability.build",
                             payload={"gap_id": gap_id}, priority=40, max_attempts=2)
    return inserted


def process(c, job, *, home=None, pulse=None, repo_root=None):
    from .request_fabric import Interrupted, resume, stopped
    row = c.execute("SELECT g.*,r.state request_state FROM capability_build_tasks g "
                    "JOIN execution_requests r ON r.id=g.request_id WHERE g.id=?",
                    (job.payload["gap_id"],)).fetchone()
    if (not row or row["state"] == "resolved" or row["request_state"] in {"succeeded", "cancelled"}
            or (row["state"] == "qualified" and row["request_state"] != "waiting_capability")):
        return {"state": "already_processed"}
    if stopped(c, row["request_id"]):
        return {"state": "paused"}

    def check():
        if pulse:
            pulse()
        if stopped(c, row["request_id"]):
            raise Interrupted("Owner paused capability recovery; original receipts retained")

    check()
    if row["capability"] == recipes.ARTIFACT_CAPABILITY:
        try:
            receipt = recipes.repair(c, row["capability"], home=_home(c, home), pulse=check)
            check()
        except recipes.RecipeAuthorityBlocked:
            capability = c.execute("SELECT updated_at FROM capabilities WHERE name=?",
                                   (row["capability"],)).fetchone()
            _save(c, row, "authority_blocked", {"recipe": recipes.RECIPE, "qualified": False,
                  "registry_revision": capability[0] if capability else None,
                  "detail": "Explicit capability authority gate retained"})
            return {"state": "authority_blocked", "gap_id": row["id"]}
        except (OSError, ValueError, TimeoutError) as exc:
            _save(c, row, "waiting_local_recipe", {"recipe": recipes.RECIPE, "qualified": False,
                                                    "error_class": type(exc).__name__})
            return {"state": "waiting_local_recipe", "gap_id": row["id"]}
        # Qualified is deliberately different from resolved: only the original
        # request's verified completion can resolve this capability gap.
        resumed = False
        try:
            c.execute("UPDATE capability_build_tasks SET state='qualified',recipe_receipt_json=?,updated_at=? WHERE id=?",
                      (json.dumps(receipt, sort_keys=True), time.time(), row["id"]))
            if c.execute("SELECT state FROM execution_requests WHERE id=?", (row["request_id"],)).fetchone()[0] == "waiting_capability":
                # resume/enqueue commits qualification and the original request
                # generation together, so a crash cannot strand a qualified gap.
                resume(c, row["request_id"])
                resumed = True
            else:
                c.commit()
        except BaseException:
            c.rollback()
            raise
        if resumed:
            append_event(c, "capability.local_recipe.request_resumed", {
                "gap_id": row["id"], "request_id": row["request_id"], "recipe": recipes.RECIPE,
            })
        return {"state": "qualified", "gap_id": row["id"], "request_id": row["request_id"]}

    if row["capability"] in {"ai.authentication", "sandbox.authorization"}:
        return {"state": "authority_blocked", "gap_id": row["id"]}

    # Preserve older draft text and any previous run binding. Cross-request and
    # crash-before-binding reconciliation also reuse completed/failed evidence;
    # this factory never mints successive repairs of the same root objective.
    goal = _objective(row)
    run = c.execute("SELECT id,state,commit_sha FROM engineering_runs WHERE id=?",
                    (row["engineering_run_id"],)).fetchone() if row["engineering_run_id"] else None
    if run is None:
        run = c.execute("SELECT id,state,commit_sha FROM engineering_runs WHERE goal=? AND acceptance_json=? "
                        "ORDER BY id LIMIT 1", (goal, json.dumps(ACCEPTANCE))).fetchone()
    if run is None:
        try:
            result = engineering.submit(c, goal=goal, acceptance=ACCEPTANCE, home=_home(c, home),
                                        repo_root=repo_root, priority=80, pulse=check)
            run_id = result["id"]
        except (CapabilityUnavailable, AdapterError, OSError, ValueError, TimeoutError) as exc:
            check()
            _save(c, row, "waiting_engineering_source", {
                "qualified": False, "error_class": type(exc).__name__,
                "detail": "Authoritative engineering source unavailable; no stale base or owner wait used",
            })
            return {"state": "waiting_engineering_source", "gap_id": row["id"]}
    else:
        run_id = run["id"]
    check()
    c.execute("UPDATE capability_build_tasks SET engineering_run_id=? WHERE id=?", (run_id, row["id"]))
    _save(c, row, "engineering_queued", {"engineering_run_id": run_id, "qualified": False,
                                        "detail": "Branch/test qualification path; activation still governed"})
    append_event(c, "capability.engineering.linked", {
        "gap_id": row["id"], "request_id": row["request_id"], "engineering_run_id": run_id,
    })
    return {"state": "engineering_queued", "gap_id": row["id"], "engineering_run_id": run_id}


def schedule_waiting(c, *, interval_seconds=300):
    """Cheap durable waits: all qualification/Git work stays in the heavy lane."""
    from .request_fabric import stopped
    now = time.time()
    created = 0
    rows = c.execute("SELECT g.* FROM capability_build_tasks g JOIN execution_requests r ON r.id=g.request_id "
                     "WHERE r.state='waiting_capability' AND g.state IN "
                     "('pending','candidate_ready','waiting_local_recipe','waiting_engineering_source','authority_blocked','qualified') "
                     "AND g.capability NOT IN ('ai.authentication','sandbox.authorization') "
                     "ORDER BY g.updated_at LIMIT 10").fetchall()
    for row in rows:
        if stopped(c, row["request_id"]):
            continue
        if row["state"] == "authority_blocked":
            capability = c.execute("SELECT updated_at FROM capabilities WHERE name=?", (row["capability"],)).fetchone()
            previous = json.loads(row["recipe_receipt_json"])
            if previous.get("registry_revision") == (capability[0] if capability else None):
                continue
        elif now - row["updated_at"] < interval_seconds:
            continue
        active = c.execute("SELECT 1 FROM worker_jobs WHERE kind='capability.build' AND state IN ('queued','running','retry') "
                           "AND json_extract(payload_json,'$.gap_id')=? LIMIT 1", (row["id"],)).fetchone()
        if active:
            continue
        c.execute("UPDATE capability_build_tasks SET updated_at=? WHERE id=?", (now, row["id"]))
        created += int(enqueue_gap(c, row["id"]))
    return created
