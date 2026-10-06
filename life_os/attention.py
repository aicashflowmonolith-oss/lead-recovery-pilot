"""Durable owner-attention and approval primitives."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from .events import append_event

ATTENTION_KINDS = {
    "verified_revenue","buyer_signal","human_gate","deadline",
    "failure_unrepaired","milestone","human_executor_approval",
}
APPROVAL_STATES = {"pending","approved","denied","expired"}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _json(value: Any) -> str:
    return json.dumps(value or {}, separators=(",",":"), sort_keys=True)

def emit_attention(connection: sqlite3.Connection, *, fingerprint: str, kind: str,
                   source: str, severity: str="info", payload: dict[str,Any]|None=None,
                   correlation_id: str|None=None, event_id: str|None=None) -> tuple[int,bool]:
    if kind not in ATTENTION_KINDS:
        raise ValueError(f"unsupported attention kind: {kind}")
    cur=connection.execute(
        """INSERT OR IGNORE INTO owner_attention
        (fingerprint,kind,severity,source,correlation_id,event_id,payload_json,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (fingerprint,kind,severity,source,correlation_id,event_id,_json(payload),_now()),
    )
    row=connection.execute("SELECT id FROM owner_attention WHERE fingerprint=?",(fingerprint,)).fetchone()
    created=cur.rowcount==1
    if created:
        append_event(connection,"owner_attention.created",{"attention_id":row["id"],"kind":kind,"source":source})
    else:
        connection.commit()
    return int(row["id"]),created

def list_attention(connection: sqlite3.Connection, *, open_only: bool=True, limit: int=50) -> list[dict[str,Any]]:
    where="WHERE acknowledged_at IS NULL" if open_only else ""
    rows=connection.execute(
        f"""SELECT id,fingerprint,kind,severity,source,correlation_id,event_id,
        payload_json,created_at,acknowledged_at FROM owner_attention {where}
        ORDER BY created_at DESC,id DESC LIMIT ?""",(limit,)
    ).fetchall()
    return [dict(r) for r in rows]

def acknowledge_attention(connection: sqlite3.Connection, attention_id: int) -> bool:
    cur=connection.execute(
        "UPDATE owner_attention SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL",
        (_now(),attention_id),
    )
    connection.commit()
    if cur.rowcount:
        append_event(connection,"owner_attention.acknowledged",{"attention_id":attention_id})
    return cur.rowcount==1

def request_approval(connection: sqlite3.Connection, *, fingerprint: str, action: str,
                     risk: str, cost_cents: int=0, payload: dict[str,Any]|None=None,
                     expires_at: str|None=None) -> tuple[int,bool]:
    if cost_cents < 0:
        raise ValueError("cost_cents cannot be negative")
    cur=connection.execute(
        """INSERT OR IGNORE INTO approvals
        (fingerprint,action,risk,cost_cents,state,payload_json,created_at,expires_at)
        VALUES(?,?,?,?, 'pending',?,?,?)""",
        (fingerprint,action,risk,cost_cents,_json(payload),_now(),expires_at),
    )
    row=connection.execute("SELECT id FROM approvals WHERE fingerprint=?",(fingerprint,)).fetchone()
    created=cur.rowcount==1
    connection.commit()
    if created:
        append_event(connection,"approval.requested",{"approval_id":row["id"],"action":action,"risk":risk,"cost_cents":cost_cents})
    return int(row["id"]),created

def list_approvals(connection: sqlite3.Connection, *, state: str|None="pending", limit: int=50) -> list[dict[str,Any]]:
    if state is not None and state not in APPROVAL_STATES:
        raise ValueError("invalid approval state")
    if state is None:
        rows=connection.execute("SELECT * FROM approvals ORDER BY created_at DESC,id DESC LIMIT ?",(limit,)).fetchall()
    else:
        rows=connection.execute("SELECT * FROM approvals WHERE state=? ORDER BY created_at,id LIMIT ?",(state,limit)).fetchall()
    return [dict(r) for r in rows]

def decide_approval(connection: sqlite3.Connection, approval_id: int, decision: str) -> bool:
    if decision not in {"approved","denied"}:
        raise ValueError("decision must be approved or denied")
    row=connection.execute("SELECT fingerprint FROM approvals WHERE id=?",(approval_id,)).fetchone()
    if decision == "approved" and row is not None and row["fingerprint"].startswith("revenue-followthrough:"):
        from .revenue_followthrough import approval_is_current
        if not approval_is_current(connection,row["fingerprint"]):
            return False
    now=_now()
    cur=connection.execute(
        "UPDATE approvals SET state=?,decided_at=? WHERE id=? AND state='pending' AND (expires_at IS NULL OR expires_at>?)",
        (decision,now,approval_id,now),
    )
    connection.commit()
    if cur.rowcount:
        append_event(connection,"approval.decided",{"approval_id":approval_id,"decision":decision})
    return cur.rowcount==1

def expire_approvals(connection: sqlite3.Connection) -> int:
    now=_now()
    rows=connection.execute(
        "SELECT id FROM approvals WHERE state='pending' AND expires_at IS NOT NULL AND expires_at<=?",(now,)
    ).fetchall()
    if not rows:
        return 0
    connection.execute(
        "UPDATE approvals SET state='expired',decided_at=? WHERE state='pending' AND expires_at IS NOT NULL AND expires_at<=?",
        (now,now),
    )
    connection.commit()
    for row in rows:
        append_event(connection,"approval.expired",{"approval_id":row["id"]})
    return len(rows)
