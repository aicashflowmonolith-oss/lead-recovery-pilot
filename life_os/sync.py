"""Structured, idempotent synchronization inbox/outbox."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from .boundaries import validate_external_payload
from .events import append_event

def _now()->str:
    return datetime.now(timezone.utc).isoformat()

def _json(v:Any)->str:
    return json.dumps(v or {},separators=(",",":"),sort_keys=True)

def ingest(connection:sqlite3.Connection, *, event_id:str, schema_version:str, source:str,
           target:str, kind:str, payload:dict[str,Any]|None=None,
           correlation_id:str|None=None)->tuple[int,bool]:
    validate_external_payload(payload or {})
    cur=connection.execute(
        """INSERT OR IGNORE INTO sync_inbox
        (event_id,schema_version,source,target,kind,correlation_id,payload_json,state,created_at)
        VALUES(?,?,?,?,?,?,?,'pending',?)""",
        (event_id,schema_version,source,target,kind,correlation_id,_json(payload),_now()),
    )
    row=connection.execute("SELECT id FROM sync_inbox WHERE event_id=?",(event_id,)).fetchone()
    connection.commit()
    if cur.rowcount:
        append_event(connection,"sync.inbox.received",{"event_id":event_id,"kind":kind,"source":source})
    return int(row["id"]),cur.rowcount==1

def mark_processed(connection:sqlite3.Connection,event_id:str,error:str="")->bool:
    state="failed" if error else "processed"
    cur=connection.execute(
        "UPDATE sync_inbox SET state=?,processed_at=?,attempts=attempts+1,error=? WHERE event_id=? AND state IN ('pending','processing','failed')",
        (state,_now(),error[:2000],event_id),
    )
    connection.commit()
    return cur.rowcount==1

def emit(connection:sqlite3.Connection, *, target:str,kind:str,payload:dict[str,Any]|None=None,
         source:str="life-os",schema_version:str="life-os.sync.v1",event_id:str|None=None,
         correlation_id:str|None=None)->tuple[str,bool]:
    validate_external_payload(payload or {})
    eid=event_id or str(uuid.uuid4())
    cur=connection.execute(
        """INSERT OR IGNORE INTO sync_outbox
        (event_id,schema_version,source,target,kind,correlation_id,payload_json,state,created_at)
        VALUES(?,?,?,?,?,?,?,'pending',?)""",
        (eid,schema_version,source,target,kind,correlation_id,_json(payload),_now()),
    )
    connection.commit()
    if cur.rowcount:
        append_event(connection,"sync.outbox.emitted",{"event_id":eid,"kind":kind,"target":target})
    return eid,cur.rowcount==1

def acknowledge(connection:sqlite3.Connection,event_id:str)->bool:
    cur=connection.execute(
        "UPDATE sync_outbox SET state='acked',acked_at=? WHERE event_id=? AND state!='acked'",(_now(),event_id)
    )
    connection.commit()
    return cur.rowcount==1

def sync_status(connection:sqlite3.Connection)->dict[str,dict[str,int]]:
    def counts(table):
        return {r["state"]:r["n"] for r in connection.execute(f"SELECT state,COUNT(*) n FROM {table} GROUP BY state")}
    return {"inbox":counts("sync_inbox"),"outbox":counts("sync_outbox")}

def recover_stale(connection:sqlite3.Connection,older_than_seconds:int=300)->int:
    cutoff=(datetime.now(timezone.utc)-timedelta(seconds=older_than_seconds)).isoformat()
    a=connection.execute(
        "UPDATE sync_inbox SET state='pending',error='recovered stale processing record' WHERE state='processing' AND created_at<?",
        (cutoff,),
    ).rowcount
    b=connection.execute(
        "UPDATE sync_outbox SET state='pending',error='recovered stale sending record' WHERE state='sending' AND created_at<?",
        (cutoff,),
    ).rowcount
    connection.commit()
    if a+b:
        append_event(connection,"sync.recovered",{"count":a+b})
    return a+b


def claim_outbox(connection:sqlite3.Connection)->dict[str,Any]|None:
    row=connection.execute(
        "SELECT * FROM sync_outbox WHERE state IN ('pending','failed') ORDER BY created_at,id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur=connection.execute(
        "UPDATE sync_outbox SET state='sending',attempts=attempts+1,error='' WHERE id=? AND state IN ('pending','failed')",
        (row["id"],),
    )
    connection.commit()
    if cur.rowcount != 1:
        return None
    claimed=connection.execute("SELECT * FROM sync_outbox WHERE id=?",(row["id"],)).fetchone()
    append_event(connection,"sync.outbox.claimed",{"event_id":claimed["event_id"],"attempts":claimed["attempts"]})
    return dict(claimed)

def fail_outbox(connection:sqlite3.Connection,event_id:str,error:str)->bool:
    cur=connection.execute(
        "UPDATE sync_outbox SET state='failed',error=? WHERE event_id=? AND state='sending'",
        (error[:2000],event_id),
    )
    connection.commit()
    if cur.rowcount:
        append_event(connection,"sync.outbox.failed",{"event_id":event_id})
    return cur.rowcount==1

def claim_inbox(connection:sqlite3.Connection)->dict[str,Any]|None:
    row=connection.execute(
        "SELECT * FROM sync_inbox WHERE state IN ('pending','failed') ORDER BY created_at,id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur=connection.execute(
        "UPDATE sync_inbox SET state='processing',attempts=attempts+1,error='' WHERE id=? AND state IN ('pending','failed')",
        (row["id"],),
    )
    connection.commit()
    if cur.rowcount != 1:
        return None
    claimed=connection.execute("SELECT * FROM sync_inbox WHERE id=?",(row["id"],)).fetchone()
    append_event(connection,"sync.inbox.claimed",{"event_id":claimed["event_id"],"attempts":claimed["attempts"]})
    return dict(claimed)
