"""Explicit retirement lifecycle for replaceable capabilities."""
from __future__ import annotations
import sqlite3, uuid
from datetime import datetime,timezone
from .events import append_event
SCHEMA="""
CREATE TABLE IF NOT EXISTS decommission_plans(
 id TEXT PRIMARY KEY,capability_name TEXT NOT NULL,reason TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('planned','completed','cancelled')),
 created_at TEXT NOT NULL,completed_at TEXT NOT NULL DEFAULT '');
"""
def _now(): return datetime.now(timezone.utc).isoformat()
def initialize(c): c.executescript(SCHEMA); c.commit()
def plan(c,*,capability_name,reason):
    initialize(c); row=c.execute('SELECT enabled FROM capabilities WHERE name=?',(capability_name,)).fetchone()
    if row is None: raise ValueError('unknown capability')
    if not reason.strip(): raise ValueError('retirement reason required')
    pid=uuid.uuid4().hex; c.execute("INSERT INTO decommission_plans VALUES(?,?,?,'planned',?,'')",(pid,capability_name,reason.strip()[:1000],_now())); c.commit(); append_event(c,'capability.retirement_planned',{'plan_id':pid,'capability':capability_name}); return pid
def complete(c,plan_id,*,owner_confirmed=False):
    initialize(c)
    if not owner_confirmed: raise ValueError('explicit owner confirmation required for capability retirement')
    row=c.execute("SELECT * FROM decommission_plans WHERE id=? AND state='planned'",(plan_id,)).fetchone()
    if row is None: raise ValueError('active retirement plan not found')
    c.execute('UPDATE capabilities SET enabled=0,updated_at=? WHERE name=?',(_now(),row['capability_name']))
    c.execute("UPDATE decommission_plans SET state='completed',completed_at=? WHERE id=?",(_now(),plan_id)); c.commit(); append_event(c,'capability.retired',{'plan_id':plan_id,'capability':row['capability_name']}); return True
def cancel(c,plan_id):
    initialize(c); cur=c.execute("UPDATE decommission_plans SET state='cancelled' WHERE id=? AND state='planned'",(plan_id,)); c.commit(); return cur.rowcount==1
