"""Append-only event log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Event:
    id: int
    kind: str
    occurred_at: str
    payload: dict[str, Any]


def append_event(
    connection: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> Event:
    if not kind.strip():
        raise ValueError("event kind must not be empty")
    when = (occurred_at or datetime.now(timezone.utc)).isoformat()
    body = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
    cursor = connection.execute(
        "INSERT INTO events(kind, occurred_at, payload_json) VALUES (?, ?, ?)",
        (kind.strip(), when, body),
    )
    connection.commit()
    return Event(int(cursor.lastrowid), kind.strip(), when, json.loads(body))


def list_events(connection: sqlite3.Connection, limit: int = 100) -> list[Event]:
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = connection.execute(
        "SELECT id, kind, occurred_at, payload_json "
        "FROM events ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        Event(
            id=row["id"],
            kind=row["kind"],
            occurred_at=row["occurred_at"],
            payload=json.loads(row["payload_json"]),
        )
        for row in rows
    ]
