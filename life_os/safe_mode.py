"""Planner-independent, bounded fail-safe and recovery primitives for LIFE OS."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .backup import create_backup
from .events import append_event
from .queue import get_state, initialize_queue, set_state, stats

PAUSE_KEY = "safe_mode.paused"


def diagnose(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return deterministic database and queue health without invoking planners."""
    initialize_queue(connection)
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    rows = connection.execute(
        "SELECT state, COUNT(*) AS n FROM worker_jobs GROUP BY state"
    ).fetchall()
    counts = {str(row["state"]): int(row["n"]) for row in rows}
    return {
        "integrity": integrity,
        "queue": counts,
        "healthy": integrity == "ok",
        "safe_mode": status(connection),
    }


def set_paused(connection: sqlite3.Connection, paused: bool) -> bool:
    """Persist the dedicated safe-mode pause flag for cooperating controllers."""
    initialize_queue(connection)
    set_state(connection, PAUSE_KEY, "1" if paused else "0")
    return paused


def enter(connection: sqlite3.Connection, reason: str) -> dict[str, Any]:
    """Enter fail-safe mode and pause normal worker execution idempotently."""
    initialize_queue(connection)
    reason = reason.strip()
    if not reason:
        raise ValueError("safe-mode reason required")
    if get_state(connection, "safe_mode.active") == "1":
        return status(connection)
    previous = get_state(connection, "worker.paused") or "0"
    set_state(connection, "safe_mode.previous_worker_paused", previous)
    set_state(connection, "safe_mode.reason", reason[:1000])
    set_state(connection, "safe_mode.active", "1")
    set_paused(connection, True)
    set_state(connection, "worker.paused", "1")
    append_event(connection, "safe_mode.entered", {"reason": reason[:500]})
    return status(connection)


def exit(connection: sqlite3.Connection, *, owner_confirmed: bool = False) -> dict[str, Any]:
    """Leave fail-safe mode only after explicit owner confirmation."""
    initialize_queue(connection)
    if not owner_confirmed:
        raise ValueError("explicit owner confirmation required to exit safe mode")
    previous = get_state(connection, "safe_mode.previous_worker_paused") or "0"
    set_state(connection, "safe_mode.active", "0")
    set_state(connection, "safe_mode.reason", "")
    set_paused(connection, False)
    set_state(connection, "worker.paused", previous)
    append_event(connection, "safe_mode.exited", {"restored_worker_paused": previous})
    return status(connection)


def status(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_queue(connection)
    return {
        "active": get_state(connection, "safe_mode.active") == "1",
        "reason": get_state(connection, "safe_mode.reason") or "",
        "dedicated_pause": get_state(connection, PAUSE_KEY) == "1",
        "worker_paused": get_state(connection, "worker.paused") == "1",
        "emergency_stop": get_state(connection, "worker.emergency_stop") == "1",
        "queue": stats(connection),
    }


def diagnostics(connection: sqlite3.Connection) -> dict[str, Any]:
    """Expose recovery diagnostics without granting any execution authority."""
    from .attention import list_approvals
    from .sync import sync_status

    result = diagnose(connection)
    result.update({
        "pending_approvals": len(list_approvals(connection, state="pending", limit=100)),
        "sync": sync_status(connection),
    })
    return result


def backup(connection: sqlite3.Connection, directory: str | Path) -> Path:
    """Create and integrity-check a recovery snapshot."""
    return create_backup(connection, directory)


def restore_copy(source_backup: str | Path, destination: str | Path) -> Path:
    """Restore only to a new path; never overwrite live state."""
    source = Path(source_backup)
    target = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        if str(src.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("source backup failed integrity check")
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    verify = sqlite3.connect(target)
    try:
        if str(verify.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("restored copy failed integrity check")
    finally:
        verify.close()
    return target
