"""Central autonomy ceilings. Quotas limit actions; they never grant authority."""
from __future__ import annotations
import sqlite3
from datetime import datetime,timedelta,timezone
from .events import append_event
SCOPES={'compute_tokens','external_messages','machine_actions','money_cents','risk_points'}
SCHEMA="""
CREATE TABLE IF NOT EXISTS quota_budgets(
 scope TEXT PRIMARY KEY,window_seconds INTEGER NOT NULL CHECK(window_seconds>0),
 limit_units REAL NOT NULL CHECK(limit_units>=0),used_units REAL NOT NULL DEFAULT 0 CHECK(used_units>=0),
 window_started_at TEXT NOT NULL,updated_at TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)));
"""
def _now(): return datetime.now(timezone.utc)
def initialize(c): c.executescript(SCHEMA); c.commit()
def configure(c,*,scope,limit_units,window_seconds,enabled=True,now=None):
    initialize(c)
    if scope not in SCOPES or float(limit_units)<0 or int(window_seconds)<1: raise ValueError('invalid quota')
    when=(now or _now()).astimezone(timezone.utc).isoformat()
    c.execute("INSERT INTO quota_budgets(scope,window_seconds,limit_units,used_units,window_started_at,updated_at,enabled) VALUES(?,?,?,0,?,?,?) ON CONFLICT(scope) DO UPDATE SET window_seconds=excluded.window_seconds,limit_units=excluded.limit_units,enabled=excluded.enabled,updated_at=excluded.updated_at",(scope,int(window_seconds),float(limit_units),when,when,int(enabled))); c.commit()
def _refresh(c,row,now):
    start=datetime.fromisoformat(row['window_started_at']); elapsed=(now-start).total_seconds()
    if elapsed>=row['window_seconds']:
        c.execute("UPDATE quota_budgets SET used_units=0,window_started_at=?,updated_at=? WHERE scope=?",(now.isoformat(),now.isoformat(),row['scope'])); c.commit(); return c.execute("SELECT * FROM quota_budgets WHERE scope=?",(row['scope'],)).fetchone()
    return row
def consume(c,*,scope,units=1.0,now=None)->dict:
    initialize(c); units=float(units)
    if scope not in SCOPES or units<0: raise ValueError('invalid quota consumption')
    row=c.execute("SELECT * FROM quota_budgets WHERE scope=?",(scope,)).fetchone()
    if row is None: return {'allowed':False,'reason':'quota_not_configured','scope':scope,'remaining':0.0}
    current=(now or _now()).astimezone(timezone.utc); row=_refresh(c,row,current)
    remaining=max(0.0,float(row['limit_units'])-float(row['used_units']))
    if not row['enabled'] or units>remaining:
        append_event(c,'quota.denied',{'scope':scope,'units':units,'remaining':remaining}); return {'allowed':False,'reason':'quota_exhausted_or_disabled','scope':scope,'remaining':remaining}
    c.execute("UPDATE quota_budgets SET used_units=used_units+?,updated_at=? WHERE scope=?",(units,current.isoformat(),scope)); c.commit()
    return {'allowed':True,'scope':scope,'remaining':max(0.0,remaining-units),'authority_granted':False}
def snapshot(c):
    initialize(c); return [dict(r) for r in c.execute("SELECT * FROM quota_budgets ORDER BY scope")]
