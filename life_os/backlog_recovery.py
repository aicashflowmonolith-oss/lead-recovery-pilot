"""Bounded native recovery of an explicitly handed-off engineering backlog.

Uses existing policy, run, event and job tables. No new queue or model supervisor.
One successor per source run and handoff revision; preserved failures remain audit
evidence. Promotion and runtime activation do not follow from this module.
"""
from __future__ import annotations

import json
from pathlib import Path

from .ai_cli import AdapterError, CapabilityUnavailable
from .engineering import get_run, submit
from .queue import get_state

POLICY_KEY = "engineering_handoff"
COMMAND = "reconcile engineering backlog"


def reconcile(connection, *, home: Path):
    row = connection.execute("SELECT value_json FROM brain_policy_state WHERE key=?", (POLICY_KEY,)).fetchone()
    if row is None:
        return {"state": "no_handoff", "created": 0}
    policy = json.loads(row["value_json"])
    revision = str(policy["revision"])
    sources = policy["source_run_ids"]
    if not isinstance(sources, list) or len(sources) > 100 or any(type(i) is not int for i in sources):
        raise ValueError("handoff requires at most 100 integer run references")
    if policy.get("activation_state") != "active":
        return {"state": "awaiting_activation", "created": 0, "source_runs": sources}
    approval = connection.execute("SELECT action,state,payload_json FROM approvals WHERE id=?",
                                  (policy.get("approval_id"),)).fetchone()
    if not approval or approval["state"] != "approved" or approval["action"] != "ACTIVATE_ENGINEERING_HANDOFF":
        return {"state": "awaiting_activation", "created": 0}
    authorized = json.loads(approval["payload_json"])
    if authorized.get("revision") != revision or authorized.get("runtime_commit") != policy.get("runtime_commit"):
        return {"state": "awaiting_activation", "created": 0}
    from .objective_lineage import qualified_lineage, stopped, _authorized, _snapshot
    lineage = qualified_lineage(connection)
    if lineage.snapshot is None or not _authorized(lineage.snapshot):
        return {"state": "awaiting_activation", "created": 0}
    if stopped(connection):
        return {"state": "paused", "created": 0}
    # Never compete with existing heavy work; independent worker jobs continue.
    active = connection.execute("SELECT 1 FROM worker_jobs WHERE kind='engineering.build' "
                                "AND state IN ('queued','running','retry') LIMIT 1").fetchone()
    if active:
        return {"state": "engineering_active", "created": 0}
    receipts_key = f"engineering.handoff.receipts:{revision}"
    receipts = json.loads(get_state(connection, receipts_key) or "{}")
    for source_id in sources:
        if source_id in lineage.blocked:
            continue
        if str(source_id) in receipts:
            continue
        source = get_run(connection, source_id)
        if not source or source["state"] not in {"failed", "waiting_provider"}:
            continue
        # This slice recovers the diagnosed no-edit/provider failures only.
        if not any(text in source["failure_reason"].lower() for text in (
            "no repository changes", "provider", "code-mode", "sandbox", "output exceeded",
        )):
            continue
        repo = Path(source["repo_root"])
        try:
            if not lineage.current(connection):
                return {"state": "awaiting_activation", "created": 0}
            required = policy.get("required_base_commits", {}).get(str(source_id))
            acceptance = list(source["acceptance"])
            acceptance.append(f"Recovery of engineering run {source_id}; handoff {revision}; preserve original evidence")
            result = submit(connection, goal=source["goal"], acceptance=acceptance,
                            home=home, repo_root=repo, priority=85, required_base_commit=required,
                            handoff_source_id=source_id)
        except (CapabilityUnavailable, AdapterError, OSError, ValueError):
            # A branch-specific source/base failure cannot stop the other repo.
            continue
        # A different source may have restored its receipt while submit proved
        # Git identity. Merge the current ledger under one write lock; never
        # replace it from this reconciler's stale whole-map read.
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _snapshot(connection)
            if (stopped(connection) or current is None or not _authorized(current)
                    or current["policy"] != lineage.snapshot["policy"]
                    or current["approval"] != lineage.snapshot["approval"]):
                connection.rollback()
                return {"state": "awaiting_activation", "created": 0}
            original_source = next((row for row in lineage.snapshot["rows"] if row[0] == source_id), None)
            current_source = next((row for row in current["rows"] if row[0] == source_id), None)
            if original_source != current_source:
                connection.rollback()
                return {"state": "waiting_reconciliation", "created": 0}
            receipts = json.loads(current["receipts"])
            proposed = {"successor_run_id": result["id"], "base_commit": result["base_commit"]}
            previous = receipts.get(str(source_id))
            if previous is not None and previous != proposed:
                connection.rollback()
                return {"state": "waiting_reconciliation", "created": 0}
            if previous is None:
                receipts[str(source_id)] = proposed
                # Use the existing receipt/event ledger atomically. set_state
                # and append_event each commit, so use their underlying writes.
                from .queue import _iso
                now = _iso()
                connection.execute("INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                                   (receipts_key, json.dumps(receipts, sort_keys=True), now))
                connection.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
                                   ("engineering.handoff.successor", now, json.dumps({
                                       "revision": revision, "source_run_id": source_id, "run_id": result["id"]})))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return {"state": "queued", "created": int(result["created"]), "source_run_id": source_id,
                "run_id": result["id"]}
    return {"state": "waiting_reconciliation", "created": 0, "receipts": receipts}
