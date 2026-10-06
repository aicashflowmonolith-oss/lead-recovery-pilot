"""Desired-state reconciliation primitives for LIFE OS controllers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .events import append_event

STATUSES = {"pending", "reconciling", "in_sync", "degraded", "blocked", "failed"}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["desired"] = json.loads(item.pop("desired_json"))
    item["observed"] = json.loads(item.pop("observed_json"))
    return item


def get_resource(connection: sqlite3.Connection, resource_key: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM desired_resources WHERE resource_key=?", (resource_key,)
    ).fetchone()
    return None if row is None else _decode(row)



def upsert_desired(
    connection: sqlite3.Connection,
    *,
    resource_key: str,
    resource_type: str,
    controller: str,
    desired: dict[str, Any],
    max_attempts: int = 5,
) -> tuple[dict[str, Any], bool]:
    if not resource_key.strip() or not resource_type.strip() or not controller.strip():
        raise ValueError("resource_key, resource_type and controller are required")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    key = resource_key.strip()
    desired_json = _json(desired)
    now = _now()
    row = connection.execute(
        "SELECT * FROM desired_resources WHERE resource_key=?", (key,)
    ).fetchone()

    if row is None:
        connection.execute(
            """INSERT INTO desired_resources(
                resource_key,resource_type,controller,desired_json,observed_json,
                generation,observed_generation,status,attempts,max_attempts,
                next_retry_at,last_error,created_at,updated_at
            ) VALUES(?,?,?,?,'{}',1,0,'pending',0,?,NULL,'',?,?)""",
            (key, resource_type.strip(), controller.strip(), desired_json, max_attempts, now, now),
        )
        changed = True
        event_kind = "reconcile.desired_created"
    else:
        changed = (
            row["desired_json"] != desired_json
            or row["resource_type"] != resource_type.strip()
            or row["controller"] != controller.strip()
        )
        if changed:
            connection.execute(
                """UPDATE desired_resources
                SET resource_type=?,controller=?,desired_json=?,generation=generation+1,
                    status='pending',attempts=0,max_attempts=?,next_retry_at=NULL,
                    last_error='',updated_at=?
                WHERE resource_key=?""",
                (
                    resource_type.strip(),
                    controller.strip(),
                    desired_json,
                    max_attempts,
                    now,
                    key,
                ),
            )
            event_kind = "reconcile.desired_changed"
        else:
            connection.execute(
                "UPDATE desired_resources SET max_attempts=?,updated_at=? WHERE resource_key=?",
                (max_attempts, now, key),
            )
            event_kind = ""

    connection.commit()
    current = get_resource(connection, key)
    assert current is not None
    if event_kind:
        append_event(
            connection,
            event_kind,
            {
                "resource_key": key,
                "resource_type": current["resource_type"],
                "controller": current["controller"],
                "generation": current["generation"],
            },
        )
    return current, changed



def mark_reconciling(connection: sqlite3.Connection, resource_key: str) -> bool:
    now = _now()
    cur = connection.execute(
        """UPDATE desired_resources
        SET status='reconciling',updated_at=?
        WHERE resource_key=? AND status IN ('pending','degraded')""",
        (now, resource_key),
    )
    connection.commit()
    if cur.rowcount:
        append_event(connection, "reconcile.started", {"resource_key": resource_key})
    return cur.rowcount == 1


def set_observed(
    connection: sqlite3.Connection,
    *,
    resource_key: str,
    observed: dict[str, Any],
    status: str = "in_sync",
    generation: int | None = None,
) -> bool:
    if status not in STATUSES:
        raise ValueError("invalid reconciliation status")
    row = connection.execute(
        "SELECT generation FROM desired_resources WHERE resource_key=?", (resource_key,)
    ).fetchone()
    if row is None:
        return False
    observed_generation = int(row["generation"] if generation is None else generation)
    cur = connection.execute(
        """UPDATE desired_resources
        SET observed_json=?,observed_generation=?,status=?,
            last_error=CASE WHEN ?='in_sync' THEN '' ELSE last_error END,
            next_retry_at=CASE WHEN ?='in_sync' THEN NULL ELSE next_retry_at END,
            updated_at=?
        WHERE resource_key=?""",
        (
            _json(observed),
            observed_generation,
            status,
            status,
            status,
            _now(),
            resource_key,
        ),
    )
    connection.commit()
    if cur.rowcount:
        append_event(
            connection,
            "reconcile.observed",
            {
                "resource_key": resource_key,
                "status": status,
                "observed_generation": observed_generation,
            },
        )
    return cur.rowcount == 1


def record_failure(
    connection: sqlite3.Connection,
    *,
    resource_key: str,
    error: str,
    retry_delay_seconds: int = 30,
) -> str:
    row = connection.execute(
        "SELECT attempts,max_attempts FROM desired_resources WHERE resource_key=?",
        (resource_key,),
    ).fetchone()
    if row is None:
        raise KeyError(resource_key)
    attempts = int(row["attempts"]) + 1
    terminal = attempts >= int(row["max_attempts"])
    status = "failed" if terminal else "degraded"
    next_retry = None
    if not terminal:
        next_retry = (_now_dt() + timedelta(seconds=max(0, retry_delay_seconds))).isoformat()
    connection.execute(
        """UPDATE desired_resources
        SET attempts=?,status=?,next_retry_at=?,last_error=?,updated_at=?
        WHERE resource_key=?""",
        (attempts, status, next_retry, error[:4000], _now(), resource_key),
    )
    connection.commit()
    append_event(
        connection,
        "reconcile.failed",
        {"resource_key": resource_key, "status": status, "attempts": attempts},
    )
    return status



def list_resources(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise ValueError("invalid reconciliation status")
    bounded = max(1, min(int(limit), 1000))
    if status is None:
        rows = connection.execute(
            "SELECT * FROM desired_resources ORDER BY updated_at DESC,id DESC LIMIT ?",
            (bounded,),
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT * FROM desired_resources
            WHERE status=? ORDER BY updated_at DESC,id DESC LIMIT ?""",
            (status, bounded),
        ).fetchall()
    return [_decode(row) for row in rows]


def due_resources(connection: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    now = _now()
    bounded = max(1, min(int(limit), 1000))
    rows = connection.execute(
        """SELECT * FROM desired_resources
        WHERE status IN ('pending','degraded')
          AND attempts < max_attempts
          AND (next_retry_at IS NULL OR next_retry_at<=?)
        ORDER BY updated_at,id LIMIT ?""",
        (now, bounded),
    ).fetchall()
    return [_decode(row) for row in rows]


def resource_in_sync(connection: sqlite3.Connection, resource_key: str) -> bool:
    row = connection.execute(
        """SELECT desired_json,observed_json,generation,observed_generation,status
        FROM desired_resources WHERE resource_key=?""",
        (resource_key,),
    ).fetchone()
    if row is None:
        return False
    return (
        row["status"] == "in_sync"
        and int(row["generation"]) == int(row["observed_generation"])
        and row["desired_json"] == row["observed_json"]
    )
