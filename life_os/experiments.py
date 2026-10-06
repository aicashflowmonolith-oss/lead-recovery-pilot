"""Lightweight experiment lifecycle for verified learning."""
from __future__ import annotations
import json, sqlite3, uuid
from datetime import datetime, timezone
from typing import Any
from .events import append_event
SCHEMA="""
CREATE TABLE IF NOT EXISTS experiments(
 id TEXT PRIMARY KEY,hypothesis TEXT NOT NULL,intervention TEXT NOT NULL,
 success_criteria TEXT NOT NULL,stopping_rule TEXT NOT NULL,baseline_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL CHECK(status IN ('draft','running','concluded','abandoned')),
 result_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_observations(
 id INTEGER PRIMARY KEY AUTOINCREMENT,experiment_id TEXT NOT NULL REFERENCES experiments(id),
 metric TEXT NOT NULL,value REAL NOT NULL,unit TEXT NOT NULL DEFAULT '',occurred_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_experiment_observations ON experiment_observations(experiment_id,occurred_at);
"""
def _now(): return datetime.now(timezone.utc).isoformat()
def initialize(c): c.executescript(SCHEMA); c.commit()
def create(c,*,hypothesis,intervention,success_criteria,stopping_rule,baseline=None):
    initialize(c)
    values=[hypothesis,intervention,success_criteria,stopping_rule]
    if any(not isinstance(x,str) or not 1<=len(x.strip())<=2000 for x in values): raise ValueError('bounded experiment text required')
    eid=uuid.uuid4().hex; now=_now()
    c.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?, 'draft','{}',?,?)",(eid,hypothesis.strip(),intervention.strip(),success_criteria.strip(),stopping_rule.strip(),json.dumps(baseline or {},separators=(',',':'),sort_keys=True),now,now)); c.commit()
    append_event(c,'experiment.created',{'experiment_id':eid}); return eid
def start(c,eid):
    initialize(c); cur=c.execute("UPDATE experiments SET status='running',updated_at=? WHERE id=? AND status='draft'",(_now(),eid)); c.commit(); return cur.rowcount==1
def observe(c,eid,*,metric,value,unit='',occurred_at=None):
    initialize(c); row=c.execute("SELECT status FROM experiments WHERE id=?",(eid,)).fetchone()
    if not row or row['status']!='running': raise ValueError('experiment must be running')
    if not isinstance(metric,str) or not 1<=len(metric.strip())<=120: raise ValueError('metric required')
    cur=c.execute("INSERT INTO experiment_observations(experiment_id,metric,value,unit,occurred_at) VALUES(?,?,?,?,?)",(eid,metric.strip(),float(value),str(unit)[:40],occurred_at or _now())); c.commit(); return int(cur.lastrowid)
def conclude(c,eid,*,result,status='concluded'):
    initialize(c)
    if status not in {'concluded','abandoned'}: raise ValueError('invalid final experiment status')
    if not isinstance(result,dict): raise ValueError('structured result required')
    cur=c.execute("UPDATE experiments SET status=?,result_json=?,updated_at=? WHERE id=? AND status IN ('draft','running')",(status,json.dumps(result,separators=(',',':'),sort_keys=True),_now(),eid)); c.commit()
    if cur.rowcount: append_event(c,'experiment.finished',{'experiment_id':eid,'status':status})
    return cur.rowcount==1
