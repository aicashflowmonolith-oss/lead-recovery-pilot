"""Durable exact-owner holds for unverified process exit; no model required."""
from __future__ import annotations

import json
import os
import time

from .queue import get_state, set_state

HOLDS_KEY = "recovery.containment_holds"


def read_holds(connection):
    raw = get_state(connection, HOLDS_KEY)
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Containment evidence is invalid; preserve running leases")
    return value


def hold_owner(connection, worker_id, *, pid, identities=None, snapshot_complete=False):
    connection.execute("BEGIN IMMEDIATE")
    try:
        holds = read_holds(connection)
        previous = holds.get(worker_id, {})
        known = {(item["pid"], item["creation_time"]): item for item in previous.get("identities", [])}
        for item in identities or []:
            known[(item["pid"], item["creation_time"])] = item
        holds[worker_id] = {"worker_id": worker_id, "pid": pid,
                            "identities": list(known.values()),
                            "snapshot_complete": bool(snapshot_complete),
                            "observed_epoch": time.time(), "state": "awaiting_exact_exit"}
        set_state(connection, HOLDS_KEY, json.dumps(holds, sort_keys=True))
    except BaseException:
        connection.rollback()
        raise


def clear_hold(connection, worker_id):
    connection.execute("BEGIN IMMEDIATE")
    try:
        holds = read_holds(connection)
        if worker_id in holds:
            previous = holds.pop(worker_id)
            set_state(connection, HOLDS_KEY, json.dumps(holds, sort_keys=True))
            from .events import append_event
            append_event(connection, "recovery.containment_verified", previous)
        else:
            connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _identity_exited(item, *, deadline):
    if (not isinstance(item, dict) or type(item.get("pid")) is not int or item["pid"] <= 0
            or type(item.get("creation_time")) is not int or time.monotonic() >= deadline):
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, item["pid"])
    if not handle:
        return ctypes.get_last_error() == 87
    try:
        created, exited, kernel_time, user_time = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return False
        identity = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if identity != item["creation_time"]:
            return True
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)

def exact_root_exit_verified(hold):
    """Prove only the recorded root identity exited; never infer descendant exit."""
    if os.name != "nt":
        return False
    pid = hold.get("pid")
    identities = hold.get("identities")
    if type(pid) is not int or not isinstance(identities, list):
        return False
    root = next((item for item in identities if isinstance(item, dict) and item.get("pid") == pid), None)
    return root is not None and _identity_exited(root, deadline=time.monotonic() + 2)

def exact_exit_verified(hold):
    """Inspect exact process identities; PID reuse proves the old process exited.

    Never kill a persisted PID. Missing/partial evidence cannot qualify full-tree exit.
    A timestamp delay or an empty job accounting count is not verification.
    """
    identities = hold.get("identities")
    if hold.get("snapshot_complete") is not True or not isinstance(identities, list) or not identities:
        return False
    if os.name != "nt" or len(identities) > 65536:
        return False
    deadline = time.monotonic() + 2
    return all(_identity_exited(item, deadline=deadline) for item in identities)


def recover_verified_holds(connection):
    if any(get_state(connection, key) == "1" for key in
           ("worker.paused", "worker.emergency_stop", "safe_mode.paused")):
        return 0
    from .recovery_controller import recover_owned_jobs
    recovered = 0
    for worker_id, hold in read_holds(connection).items():
        if hold.get("worker_id") != worker_id or not exact_exit_verified(hold):
            continue
        resumed = recover_owned_jobs(connection, hold["pid"], worker_id=worker_id, verified_exit=True,
                                     expected_hold=hold)
        if resumed is not None:
            recovered += resumed
    return recovered
REPLAY_SAFE_PERIODIC_HELD_KINDS = frozenset({"reality.scan", "performance.scan"})

def supersede_replay_safe_periodic_holds(connection):
    """Release only expired replay-safe periodic work after exact root exit.

    Incomplete descendant evidence remains preserved in the immutable event ledger.
    This never applies to engineering, revenue, command, or other consequential jobs.
    """
    if any(get_state(connection, key) == "1" for key in
           ("worker.paused", "worker.emergency_stop", "safe_mode.paused")):
        return 0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    superseded = 0
    for worker_id, hold in list(read_holds(connection).items()):
        if hold.get("snapshot_complete") is True or hold.get("worker_id") != worker_id:
            continue
        rows = connection.execute(
            """SELECT id,kind,lease_expires_at FROM worker_jobs
               WHERE state='running' AND lease_owner=?""", (worker_id,)).fetchall()
        if len(rows) != 1:
            continue
        job = rows[0]
        if (job["kind"] not in REPLAY_SAFE_PERIODIC_HELD_KINDS or not job["lease_expires_at"]
                or job["lease_expires_at"] > now or not exact_root_exit_verified(hold)):
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = read_holds(connection)
            if current.get(worker_id) != hold:
                connection.rollback()
                continue
            cursor = connection.execute(
                """UPDATE worker_jobs SET state='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                   updated_at=?,last_error=? WHERE id=? AND state='running' AND lease_owner=?""",
                (now, "superseded replay-safe periodic job after exact root exit; incomplete containment preserved in event ledger",
                 job["id"], worker_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                continue
            current.pop(worker_id, None)
            connection.execute(
                """INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (HOLDS_KEY, json.dumps(current, sort_keys=True), now),
            )
            payload = {"worker_id": worker_id, "job_id": job["id"], "kind": job["kind"],
                       "root_exit_verified": True, "full_snapshot_complete": False,
                       "original_hold": hold}
            connection.execute(
                "INSERT INTO worker_job_events(job_id,kind,occurred_at,payload_json) VALUES(?,?,?,?)",
                (job["id"], "recovery.replay_safe_periodic_superseded", now, json.dumps(payload, sort_keys=True)),
            )
            connection.execute(
                "INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
                ("recovery.replay_safe_periodic_superseded", now, json.dumps(payload, sort_keys=True)),
            )
            connection.commit()
            superseded += 1
        except BaseException:
            connection.rollback()
            raise
    return superseded
