"""Conservative local self-healing for autonomy/control-plane state."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from .attention import expire_approvals, emit_attention
from .capabilities import probe_local_ai
from .events import append_event
from .sync import recover_stale, sync_status


def capability_probe(connection:sqlite3.Connection)->dict[str,Any]:
    providers=probe_local_ai(connection)
    return {"providers":providers}


def _operational_health(connection: sqlite3.Connection) -> dict[str, Any]:
    database = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not database:
        return {"healthy": True, "skipped": "non_durable_database"}
    home = Path(database).resolve().parent
    # The installed runtime owns ~/.life-os. Tests and disposable databases do
    # not acquire host/process recovery authority merely by importing this module.
    enabled = home.name == ".life-os" or os.environ.get("LIFE_OS_ENABLE_OPERATIONAL_INVARIANTS") == "1"
    if not enabled:
        return {"healthy": True, "skipped": "runtime_not_installed"}
    from .operational_invariants import scan
    return scan(connection, home=home)


def autonomy_health(connection:sqlite3.Connection)->dict[str,Any]:
    expired=expire_approvals(connection)
    recovered=recover_stale(connection,older_than_seconds=300)
    status=sync_status(connection)
    failed_in=status["inbox"].get("failed",0)
    failed_out=status["outbox"].get("failed",0)
    if failed_in+failed_out:
        emit_attention(
            connection,fingerprint=f"sync_failures:{failed_in}:{failed_out}",
            kind="failure_unrepaired",severity="warning",source="life-os.autonomy_health",
            payload={"sync_inbox_failed":failed_in,"sync_outbox_failed":failed_out},
        )
    operational=_operational_health(connection)
    result={"expired_approvals":expired,"recovered_sync":recovered,"sync":status,"operational":operational}
    append_event(connection,"autonomy.health_checked",{
        "expired_approvals":expired,
        "recovered_sync":recovered,
        "sync_failed":failed_in+failed_out,
        "operational_healthy":operational.get("healthy",False),
        "operational_skipped":operational.get("skipped",""),
    })
    return result
