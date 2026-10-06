"""Durable SQLite-backed job queue for the LIFE OS worker."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from .execution_lanes import HEAVY_JOB_KINDS

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','retry','succeeded','dead','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_worker_jobs_ready
ON worker_jobs(state, available_at, priority DESC, id);

CREATE INDEX IF NOT EXISTS idx_worker_jobs_lease
ON worker_jobs(state, lease_expires_at);

CREATE TABLE IF NOT EXISTS worker_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES worker_jobs(id),
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_worker_job_events_job
ON worker_job_events(job_id, id);

CREATE TABLE IF NOT EXISTS worker_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

@dataclass(frozen=True)
class Job:
    id: int
    fingerprint: str
    kind: str
    payload: dict[str, Any]
    priority: int
    state: str
    attempts: int
    max_attempts: int
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    last_error: str

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()

def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)

def initialize_queue(connection: sqlite3.Connection) -> None:
    connection.executescript(QUEUE_SCHEMA)
    connection.commit()

def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        fingerprint=row["fingerprint"],
        kind=row["kind"],
        payload=json.loads(row["payload_json"]),
        priority=row["priority"],
        state=row["state"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        last_error=row["last_error"],
    )

def _event(
    connection: sqlite3.Connection,
    job_id: int,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO worker_job_events(job_id,kind,occurred_at,payload_json) VALUES(?,?,?,?)",
        (job_id, kind, _iso(), _json(payload or {})),
    )

def enqueue(
    connection: sqlite3.Connection,
    *,
    fingerprint: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    priority: int = 50,
    max_attempts: int = 5,
    available_at: datetime | None = None,
) -> tuple[Job, bool]:
    if not fingerprint.strip() or not kind.strip():
        raise ValueError("fingerprint and kind are required")
    if not 0 <= priority <= 100:
        raise ValueError("priority must be 0..100")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    now = _iso()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO worker_jobs(
            fingerprint,kind,payload_json,priority,state,attempts,max_attempts,
            available_at,created_at,updated_at
        ) VALUES(?,?,?,?, 'queued',0,?,?,?,?)
        """,
        (
            fingerprint.strip(),
            kind.strip(),
            _json(payload or {}),
            priority,
            max_attempts,
            _iso(available_at),
            now,
            now,
        ),
    )
    created = cursor.rowcount == 1
    row = connection.execute(
        "SELECT * FROM worker_jobs WHERE fingerprint=?",
        (fingerprint.strip(),),
    ).fetchone()
    assert row is not None
    if created:
        _event(connection, row["id"], "queued", {"kind": kind, "priority": priority})
    connection.commit()
    return _to_job(row), created

def enqueue_if_kind_idle(
    connection: sqlite3.Connection, *, fingerprint: str, kind: str,
    payload: dict[str, Any] | None = None, priority: int = 50,
    max_attempts: int = 5, available_at: datetime | None = None,
) -> tuple[Job, bool]:
    """Atomically enqueue a singleton kind only when no active copy exists."""
    if not fingerprint.strip() or not kind.strip():
        raise ValueError("fingerprint and kind are required")
    if not 0 <= priority <= 100 or max_attempts < 1:
        raise ValueError("invalid queue bounds")
    now = _iso()
    cursor = connection.execute(
        """INSERT OR IGNORE INTO worker_jobs(
           fingerprint,kind,payload_json,priority,state,attempts,max_attempts,
           available_at,created_at,updated_at)
           SELECT ?,?,?,?,'queued',0,?,?,?,?
           WHERE NOT EXISTS (
             SELECT 1 FROM worker_jobs
             WHERE kind=? AND state IN ('queued','running','retry'))""",
        (fingerprint.strip(), kind.strip(), _json(payload or {}), priority,
         max_attempts, _iso(available_at), now, now, kind.strip()),
    )
    created = cursor.rowcount == 1
    row = connection.execute(
        "SELECT * FROM worker_jobs WHERE fingerprint=?", (fingerprint.strip(),)
    ).fetchone() if created else connection.execute(
        """SELECT * FROM worker_jobs WHERE kind=?
           AND state IN ('queued','running','retry')
           ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'retry' THEN 1 ELSE 2 END,id LIMIT 1""",
        (kind.strip(),),
    ).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT * FROM worker_jobs WHERE fingerprint=?", (fingerprint.strip(),)
        ).fetchone()
    assert row is not None
    if created:
        _event(connection, row["id"], "queued", {"kind": kind, "priority": priority})
    connection.commit()
    return _to_job(row), created

PERIODIC_SINGLETON_KINDS = frozenset({
    "coordinator.scan", "maintainer.scan", "performance.scan", "reality.scan",
    "revenue.engine", "brain.cycle", "revenue.followthrough",
    "coordinator.plan_snapshot", "coordinator.commitment_sweep",
    "coordinator.purchase_sweep", "maintainer.audit",
    "maintainer.autonomy_health", "maintainer.architecture_gap",
    "maintainer.backup", "maintainer.queue_health", "maintainer.capability_probe",
})

def converge_periodic_jobs(
    connection: sqlite3.Connection,
    *,
    kinds: set[str] | frozenset[str] = PERIODIC_SINGLETON_KINDS,
) -> int:
    """Cancel obsolete queued/retry periodic duplicates without deleting audit history.

    Running work is never cancelled here. If a running copy exists, all queued/retry
    copies are obsolete. Otherwise keep one retry (preferred) or the newest queued
    copy. This repairs historical backlog created by older schedulers while preserving
    every row and event for audit/recovery.
    """
    if not kinds:
        return 0
    cancelled = 0
    now = _iso()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for kind in sorted(kinds):
            rows = connection.execute(
                """SELECT id,state FROM worker_jobs
                   WHERE kind=? AND state IN ('queued','retry','running')
                   ORDER BY id""",
                (kind,),
            ).fetchall()
            if len(rows) <= 1:
                continue
            running = [row for row in rows if row["state"] == "running"]
            retries = [row for row in rows if row["state"] == "retry"]
            keep_ids = {row["id"] for row in running}
            if not keep_ids:
                keep = retries[-1] if retries else rows[-1]
                keep_ids.add(keep["id"])
            keep_id = min(keep_ids)
            for row in rows:
                if row["id"] in keep_ids or row["state"] == "running":
                    continue
                cursor = connection.execute(
                    """UPDATE worker_jobs
                       SET state='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                           updated_at=?,last_error=?
                       WHERE id=? AND state IN ('queued','retry')""",
                    (now, "superseded duplicate periodic job during bounded queue convergence", row["id"]),
                )
                if cursor.rowcount:
                    _event(connection, row["id"], "periodic_duplicate_superseded",
                           {"kind": kind, "kept_job_id": keep_id})
                    cancelled += 1
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return cancelled

def claim_next(
    connection: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int = 120,
    lane: str = "all",
) -> Job | None:
    if lane not in {"all", "light", "heavy"}:
        raise ValueError("invalid execution lane")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    now_dt = _now()
    now = _iso(now_dt)
    lease_until = _iso(now_dt + timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        lane_sql = ""
        lane_params = ()
        if lane != "all":
            lane_sql = " AND kind " + ("IN" if lane == "heavy" else "NOT IN") + " (" + ",".join("?" for _ in HEAVY_JOB_KINDS) + ")"
            lane_params = HEAVY_JOB_KINDS
        row = connection.execute(
            """
            SELECT * FROM worker_jobs
            WHERE state IN ('queued','retry') AND available_at <= ?
            """ + lane_sql + """
            ORDER BY
                priority + CASE WHEN state='retry' THEN 30 ELSE 0 END DESC,
                id ASC
            LIMIT 1
            """,
            (now, *lane_params),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        updated = connection.execute(
            """
            UPDATE worker_jobs
            SET state='running', attempts=attempts+1, lease_owner=?,
                lease_expires_at=?, updated_at=?
            WHERE id=? AND state IN ('queued','retry')
            """,
            (worker_id, lease_until, now, row["id"]),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        _event(connection, row["id"], "claimed", {"worker_id": worker_id,
               "ready_at": json.loads(row["payload_json"]).get("original_ready_at", row["available_at"])
               if row["kind"] == "engineering.build" else row["available_at"]})
        fresh = connection.execute(
            "SELECT * FROM worker_jobs WHERE id=?", (row["id"],)
        ).fetchone()
        connection.commit()
        return _to_job(fresh)
    except Exception:
        connection.rollback()
        raise

def complete(
    connection: sqlite3.Connection,
    job: Job,
    *,
    worker_id: str,
    result: dict[str, Any] | None = None,
) -> bool:
    now = _iso()
    cursor = connection.execute(
        """
        UPDATE worker_jobs
        SET state='succeeded', result_json=?, lease_owner=NULL,
            lease_expires_at=NULL, updated_at=?, last_error=''
        WHERE id=? AND state='running' AND lease_owner=?
        """,
        (_json(result or {}), now, job.id, worker_id),
    )
    if cursor.rowcount:
        _event(connection, job.id, "succeeded", result or {})
    connection.commit()
    return cursor.rowcount == 1

def fail(
    connection: sqlite3.Connection,
    job: Job,
    *,
    worker_id: str,
    error: str,
    retry_delay_seconds: int = 30,
) -> str:
    fresh = connection.execute(
        "SELECT attempts,max_attempts FROM worker_jobs WHERE id=?", (job.id,)
    ).fetchone()
    if fresh is None:
        raise KeyError(job.id)
    terminal = fresh["attempts"] >= fresh["max_attempts"]
    state = "dead" if terminal else "retry"
    available = _iso(_now() + timedelta(seconds=max(0, retry_delay_seconds)))
    cursor = connection.execute(
        """
        UPDATE worker_jobs
        SET state=?, available_at=?, lease_owner=NULL, lease_expires_at=NULL,
            updated_at=?, last_error=?
        WHERE id=? AND state='running' AND lease_owner=?
        """,
        (state, available, _iso(), error[:4000], job.id, worker_id),
    )
    if cursor.rowcount:
        _event(connection, job.id, state, {"error": error[:1000]})
    connection.commit()
    return state

def recover_orphaned(
    connection: sqlite3.Connection,
    *,
    force_all_running: bool = False,
) -> int:
    now = _iso()
    where = "state='running'" if force_all_running else "state='running' AND lease_expires_at <= ?"
    params: tuple[Any, ...] = () if force_all_running else (now,)
    rows = connection.execute(
        f"SELECT id,attempts,max_attempts,lease_owner FROM worker_jobs WHERE {where}", params
    ).fetchall()
    from .process_containment import read_holds
    held_owners = set(read_holds(connection))
    owned = json.loads(get_state(connection, "recovery.owned_workers") or "{}")
    if owned.get("containment") == "windows_job_kill_on_close":
        held_owners.update(item.get("worker_id") for item in owned.get("workers", {}).values())
    recovered = 0
    for row in rows:
        owner = row["lease_owner"]
        if owner in held_owners:
            continue
        state = "dead" if row["attempts"] >= row["max_attempts"] else "retry"
        connection.execute(
            """
            UPDATE worker_jobs
            SET state=?, available_at=?, lease_owner=NULL, lease_expires_at=NULL,
                updated_at=?, last_error=CASE
                    WHEN last_error='' THEN 'recovered after interrupted worker'
                    ELSE last_error END
            WHERE id=? AND state='running'
            """,
            (state, now, now, row["id"]),
        )
        _event(connection, row["id"], "recovered", {"state": state})
        recovered += 1
    connection.commit()
    return recovered

def dead_jobs(connection: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = connection.execute(
        """SELECT id,fingerprint,kind,payload_json,priority,state,attempts,max_attempts,
                  available_at,created_at,updated_at,last_error,result_json
           FROM worker_jobs WHERE state='dead' ORDER BY id LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload_json"]),
             "result": json.loads(row["result_json"])} for row in rows]


def dispose_dead(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    disposition: str,
    detail: str = "",
    successor: dict[str, Any] | None = None,
) -> bool:
    disposition = disposition.strip()
    if not disposition or len(disposition) > 80:
        raise ValueError("invalid dead-letter disposition")
    row = connection.execute(
        "SELECT state,result_json FROM worker_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if row is None or row["state"] != "dead":
        return False
    result = json.loads(row["result_json"] or "{}")
    result["dead_letter"] = {
        "disposition": disposition,
        "detail": detail[:1000],
        "successor": successor or {},
        "reconciled_at": _iso(),
    }
    now = _iso()
    cursor = connection.execute(
        """UPDATE worker_jobs
           SET state='cancelled',result_json=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?
           WHERE id=? AND state='dead'""",
        (_json(result), now, job_id),
    )
    if cursor.rowcount:
        _event(connection, job_id, "dead_reconciled", result["dead_letter"])
    connection.commit()
    return cursor.rowcount == 1


def set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """,
        (key, value, _iso()),
    )
    connection.commit()

def get_state(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM worker_state WHERE key=?", (key,)
    ).fetchone()
    return None if row is None else str(row["value"])

def stats(connection: sqlite3.Connection) -> dict[str, int]:
    result = {
        row["state"]: row["n"]
        for row in connection.execute(
            "SELECT state,COUNT(*) AS n FROM worker_jobs GROUP BY state"
        )
    }
    for state in ("queued","running","retry","succeeded","dead","cancelled"):
        result.setdefault(state, 0)
    return result

def get_job(connection: sqlite3.Connection, job_id: int) -> Job | None:
    row = connection.execute(
        "SELECT * FROM worker_jobs WHERE id=?", (job_id,)
    ).fetchone()
    return None if row is None else _to_job(row)

def recent_jobs(connection: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id,fingerprint,kind,priority,state,attempts,max_attempts,
               available_at,updated_at,last_error,result_json
        FROM worker_jobs ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            **dict(row),
            "result": json.loads(row["result_json"]),
        }
        for row in rows
    ]
