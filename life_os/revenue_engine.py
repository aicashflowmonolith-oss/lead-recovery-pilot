"""Deterministic economic control loop for autonomous revenue work.

This module ranks and allocates attention. It never spends money, sends messages,
accepts contracts, or claims revenue without authoritative payment evidence.
"""
from __future__ import annotations
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

STATES={"candidate","running","waiting_external","waiting_capability","waiting_human","blocked","won","lost"}
SCHEMA="""
CREATE TABLE IF NOT EXISTS revenue_candidates(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 external_ref TEXT NOT NULL UNIQUE,
 lane_key TEXT NOT NULL,
 title TEXT NOT NULL,
 expected_net_profit_cents INTEGER NOT NULL CHECK(expected_net_profit_cents >= 0),
 probability REAL NOT NULL CHECK(probability BETWEEN 0 AND 1),
 time_to_cash_hours REAL NOT NULL CHECK(time_to_cash_hours >= 0),
 repeatability REAL NOT NULL CHECK(repeatability BETWEEN 0 AND 1),
 scalability REAL NOT NULL CHECK(scalability BETWEEN 0 AND 1),
 operational_burden REAL NOT NULL CHECK(operational_burden BETWEEN 0 AND 1),
 risk REAL NOT NULL CHECK(risk BETWEEN 0 AND 1),
 evidence_confidence REAL NOT NULL CHECK(evidence_confidence BETWEEN 0 AND 1),
 startup_cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(startup_cost_cents >= 0),
 score REAL NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'candidate',
 human_gate TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
"""
SCHEMA += """
CREATE INDEX IF NOT EXISTS idx_revenue_candidates_rank
ON revenue_candidates(state,score DESC,updated_at DESC);
CREATE TABLE IF NOT EXISTS revenue_allocations(
 candidate_id INTEGER PRIMARY KEY REFERENCES revenue_candidates(id) ON DELETE CASCADE,
 points INTEGER NOT NULL CHECK(points BETWEEN 0 AND 100),
 reason TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revenue_executions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 candidate_id INTEGER NOT NULL REFERENCES revenue_candidates(id),
 action TEXT NOT NULL,
 outcome TEXT NOT NULL DEFAULT '',
 evidence_ref TEXT NOT NULL DEFAULT '',
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
 occurred_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_revenue_executions_candidate
ON revenue_executions(candidate_id,occurred_at DESC);
CREATE TABLE IF NOT EXISTS revenue_capability_waits(
 candidate_id INTEGER PRIMARY KEY REFERENCES revenue_candidates(id) ON DELETE CASCADE,
 required_action TEXT NOT NULL, capability_kind TEXT NOT NULL DEFAULT '', blocker TEXT NOT NULL DEFAULT '',
 resume_state TEXT NOT NULL CHECK(resume_state IN ('candidate','running')),
 first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

def _now()->str:
    return datetime.now(timezone.utc).isoformat()

def initialize(c:sqlite3.Connection)->None:
    c.executescript(SCHEMA)
    c.commit()

def _unit(value:Any,name:str)->float:
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)

def _nonnegative(value:Any,name:str)->float:
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise ValueError(f"{name} must be nonnegative")
    return float(value)
def score(*,expected_net_profit_cents:int,probability:float,time_to_cash_hours:float,
          repeatability:float,scalability:float,operational_burden:float,risk:float,
          evidence_confidence:float,startup_cost_cents:int=0)->float:
    if type(expected_net_profit_cents) is not int or expected_net_profit_cents<0:
        raise ValueError("expected_net_profit_cents must be nonnegative whole cents")
    if type(startup_cost_cents) is not int or startup_cost_cents<0:
        raise ValueError("startup_cost_cents must be nonnegative whole cents")
    p=_unit(probability,"probability"); rep=_unit(repeatability,"repeatability")
    scale=_unit(scalability,"scalability"); burden=_unit(operational_burden,"operational_burden")
    danger=_unit(risk,"risk"); confidence=_unit(evidence_confidence,"evidence_confidence")
    hours=_nonnegative(time_to_cash_hours,"time_to_cash_hours")
    expected_dollars=(expected_net_profit_cents/100.0)*p*confidence
    leverage=.5+(.25*rep)+(.25*scale)
    friction=(1.0+(hours/24.0))*(1.0+danger+burden+(startup_cost_cents/10000.0))
    return round(expected_dollars*leverage/friction,6)

def register_candidate(c:sqlite3.Connection,*,external_ref:str,lane_key:str,title:str,
                       expected_net_profit_cents:int,probability:float,time_to_cash_hours:float,
                       repeatability:float,scalability:float,operational_burden:float,risk:float,
                       evidence_confidence:float,startup_cost_cents:int=0,state:str="candidate",
                       human_gate:str="")->int:
    if state not in STATES: raise ValueError("invalid revenue candidate state")
    if state=="waiting_capability": raise ValueError("use wait_for_capability for capability waits")
    for value,name in ((external_ref,"external_ref"),(lane_key,"lane_key"),(title,"title")):
        if not isinstance(value,str) or not value.strip(): raise ValueError(f"{name} required")
    result=score(expected_net_profit_cents=expected_net_profit_cents,probability=probability,
        time_to_cash_hours=time_to_cash_hours,repeatability=repeatability,scalability=scalability,
        operational_burden=operational_burden,risk=risk,evidence_confidence=evidence_confidence,
        startup_cost_cents=startup_cost_cents)
    now=_now()
    with c:
        c.execute("""INSERT INTO revenue_candidates(external_ref,lane_key,title,expected_net_profit_cents,
          probability,time_to_cash_hours,repeatability,scalability,operational_burden,risk,evidence_confidence,
          startup_cost_cents,score,state,human_gate,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(external_ref) DO UPDATE SET lane_key=excluded.lane_key,title=excluded.title,
          expected_net_profit_cents=excluded.expected_net_profit_cents,probability=excluded.probability,
          time_to_cash_hours=excluded.time_to_cash_hours,repeatability=excluded.repeatability,
          scalability=excluded.scalability,operational_burden=excluded.operational_burden,risk=excluded.risk,
          evidence_confidence=excluded.evidence_confidence,startup_cost_cents=excluded.startup_cost_cents,
          score=excluded.score,
          state=CASE WHEN revenue_candidates.state IN ('waiting_human','blocked','won','lost') THEN revenue_candidates.state ELSE excluded.state END,
          human_gate=CASE WHEN revenue_candidates.state='waiting_human' THEN revenue_candidates.human_gate ELSE excluded.human_gate END,
          updated_at=excluded.updated_at""",
          (external_ref.strip(),lane_key.strip(),title.strip(),expected_net_profit_cents,probability,
           time_to_cash_hours,repeatability,scalability,operational_burden,risk,evidence_confidence,
           startup_cost_cents,result,state,human_gate.strip(),now,now))
        row=c.execute("SELECT id FROM revenue_candidates WHERE external_ref=?",(external_ref.strip(),)).fetchone()
        c.execute("DELETE FROM revenue_capability_waits WHERE candidate_id=?",(row["id"],))
        c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
                  ("revenue.candidate.scored",now,json.dumps({"candidate_id":row["id"],"score":result,"lane":lane_key},sort_keys=True)))
    return int(row["id"])

def ranked(c:sqlite3.Connection,limit:int=20)->list[dict[str,Any]]:
    if type(limit) is not int or not 1<=limit<=100: raise ValueError("limit must be 1..100")
    rows=c.execute("""SELECT * FROM revenue_candidates
      WHERE state IN ('candidate','running','waiting_external','waiting_capability','waiting_human')
      ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'candidate' THEN 1 WHEN 'waiting_external' THEN 2 WHEN 'waiting_capability' THEN 3 ELSE 4 END,
               score DESC,id ASC LIMIT ?""",(limit,)).fetchall()
    return [dict(r) for r in rows]
def allocate(c:sqlite3.Connection,total_points:int=100,max_per_candidate:int=50,*,max_per_lane:int=60)->list[dict[str,Any]]:
    if type(total_points) is not int or not 1<=total_points<=100: raise ValueError("total_points must be 1..100")
    if type(max_per_candidate) is not int or not 1<=max_per_candidate<=100: raise ValueError("max_per_candidate must be 1..100")
    if type(max_per_lane) is not int or not 1<=max_per_lane<=100: raise ValueError("max_per_lane must be 1..100")
    # Give each executable lane a place before taking additional candidates from
    # one lane. Waiting voice/approval work cannot starve independent text work.
    candidates=[dict(r) for r in c.execute("""WITH eligible AS (
      SELECT *,ROW_NUMBER() OVER(PARTITION BY lane_key ORDER BY score DESC,id) lane_rank
      FROM revenue_candidates WHERE state IN ('candidate','running') AND score>0 AND human_gate='')
      SELECT * FROM eligible ORDER BY lane_rank,score DESC,id LIMIT 20""")]
    c.execute("DELETE FROM revenue_allocations")
    if not candidates:
        c.commit(); return []
    weights={r["id"]:max(r["score"],0.000001) for r in candidates}
    allocations={r["id"]:0 for r in candidates}
    lane_points={r['lane_key']:0 for r in candidates}
    lane_cap=max_per_lane if len(lane_points)>1 else total_points
    for r in candidates[:min(len(candidates),total_points)]:
        if lane_points[r['lane_key']]<lane_cap:
            allocations[r["id"]]=1
            lane_points[r['lane_key']]+=1
    remaining=total_points-sum(allocations.values())
    while remaining>0:
        eligible=[r for r in candidates if allocations[r["id"]]<max_per_candidate and lane_points[r['lane_key']]<lane_cap]
        if not eligible: break
        best=max(eligible,key=lambda r:(weights[r["id"]]/(allocations[r["id"]]+1),r["score"],-r["id"]))
        allocations[best["id"]]+=1; lane_points[best['lane_key']]+=1; remaining-=1
    now=_now()
    for r in candidates:
        points=allocations[r["id"]]
        if points:
            reason=f"score={r['score']:.3f}; state={r['state']}; zero-cost-first={r['startup_cost_cents']==0}"
            c.execute("INSERT INTO revenue_allocations(candidate_id,points,reason,updated_at) VALUES(?,?,?,?)",
                      (r["id"],points,reason,now))
    c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
              ("revenue.portfolio.allocated",now,json.dumps({"points":sum(allocations.values()),"requested_points":total_points,"planning_only":True,"candidates":sum(1 for v in allocations.values() if v)},sort_keys=True)))
    c.commit()
    return [dict(r) for r in c.execute("""SELECT a.*,c.external_ref,c.lane_key,c.title,c.score
        FROM revenue_allocations a JOIN revenue_candidates c ON c.id=a.candidate_id
        ORDER BY a.points DESC,c.score DESC,c.id""")]

def set_candidate_state(c:sqlite3.Connection,candidate_id:int,state:str,*,human_gate:str="")->None:
    if state not in STATES: raise ValueError("invalid revenue candidate state")
    if state=="waiting_capability": raise ValueError("use wait_for_capability for capability waits")
    if state=="waiting_human" and not human_gate.strip(): raise ValueError("human gate description required")
    with c:
        cur=c.execute("UPDATE revenue_candidates SET state=?,human_gate=?,updated_at=? WHERE id=?",
                      (state,human_gate.strip(),_now(),candidate_id))
        if cur.rowcount!=1: raise ValueError("unknown revenue candidate")
        c.execute("DELETE FROM revenue_capability_waits WHERE candidate_id=?",(candidate_id,))
def wait_for_capability(c:sqlite3.Connection,candidate_id:int,required_action:str,*,capability_kind:str="",blocker:str="")->None:
    import re
    if not isinstance(required_action,str) or not re.fullmatch(r"[a-z0-9_.-]{1,80}",required_action):
        raise ValueError("required_action must be a stable action key")
    if not isinstance(capability_kind,str) or len(capability_kind)>80: raise ValueError("invalid capability_kind")
    if not isinstance(blocker,str) or len(blocker)>1000: raise ValueError("invalid blocker")
    row=c.execute("SELECT state FROM revenue_candidates WHERE id=?",(candidate_id,)).fetchone()
    if row is None: raise ValueError("unknown revenue candidate")
    if row["state"] in {"waiting_human","blocked","won","lost"}:
        raise ValueError("capability wait cannot replace an owner gate or terminal state")
    resume_state="running" if row["state"]=="running" else "candidate"
    existing=c.execute("SELECT resume_state FROM revenue_capability_waits WHERE candidate_id=?",(candidate_id,)).fetchone()
    if existing is not None and row["state"]=="waiting_capability": resume_state=existing["resume_state"]
    now=_now()
    with c:
        c.execute("UPDATE revenue_candidates SET state='waiting_capability',human_gate='',updated_at=? WHERE id=?",(now,candidate_id))
        c.execute("""INSERT INTO revenue_capability_waits(candidate_id,required_action,capability_kind,blocker,resume_state,first_seen_at,updated_at)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET required_action=excluded.required_action,
          capability_kind=excluded.capability_kind,blocker=excluded.blocker,resume_state=excluded.resume_state,updated_at=excluded.updated_at""",
          (candidate_id,required_action,capability_kind.strip(),blocker.strip(),resume_state,now,now))
        c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
          ("revenue.capability.waiting",now,json.dumps({"candidate_id":candidate_id,"required_action":required_action,"capability_kind":capability_kind},sort_keys=True)))

def reconcile_capability_waits(c:sqlite3.Connection)->list[dict[str,Any]]:
    from .capabilities import route_capabilities
    rows=c.execute("""SELECT w.*,c.external_ref FROM revenue_capability_waits w
      JOIN revenue_candidates c ON c.id=w.candidate_id WHERE c.state='waiting_capability' ORDER BY w.first_seen_at,w.candidate_id""").fetchall()
    resumed=[]
    for row in rows:
        routes=route_capabilities(c,required_actions=[row["required_action"]],kind=row["capability_kind"] or None,allow_owner_approval=False)
        if not routes: continue
        now=_now(); selected=routes[0]["name"]
        with c:
            c.execute("UPDATE revenue_candidates SET state=?,human_gate='',updated_at=? WHERE id=?",(row["resume_state"],now,row["candidate_id"]))
            c.execute("DELETE FROM revenue_capability_waits WHERE candidate_id=?",(row["candidate_id"],))
            c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
              ("revenue.capability.ready",now,json.dumps({"candidate_id":row["candidate_id"],"required_action":row["required_action"],"capability":selected},sort_keys=True)))
        resumed.append({"candidate_id":int(row["candidate_id"]),"external_ref":row["external_ref"],"capability":selected})
    return resumed

def record_execution(c:sqlite3.Connection,candidate_id:int,action:str,*,outcome:str="",evidence_ref:str="",verified:bool=False)->int:
    if not isinstance(action,str) or not action.strip(): raise ValueError("action required")
    if type(verified) is not bool: raise ValueError("verified must be boolean")
    if not c.execute("SELECT 1 FROM revenue_candidates WHERE id=?",(candidate_id,)).fetchone():
        raise ValueError("unknown revenue candidate")
    now=_now()
    with c:
        cur=c.execute("INSERT INTO revenue_executions(candidate_id,action,outcome,evidence_ref,verified,occurred_at) VALUES(?,?,?,?,?,?)",
                      (candidate_id,action.strip(),outcome.strip(),evidence_ref.strip(),int(verified),now))
        c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
                  ("revenue.execution.recorded",now,json.dumps({"candidate_id":candidate_id,"verified":verified},sort_keys=True)))
    return int(cur.lastrowid)

def verified_available_by_currency(c:sqlite3.Connection)->dict[str,int]:
    payments={r["currency"]:int(r["amount_cents"] or 0) for r in c.execute("""SELECT p.currency,SUM(p.amount_cents) amount_cents
      FROM revenue_receipts r JOIN payment_evidence p ON p.id=r.payment_evidence_id
      WHERE r.bank_available=1 AND p.authoritative=1 AND p.status='confirmed' AND p.evidence_kind='payment'
      GROUP BY p.currency""")}
    out={}
    for currency,amount in payments.items():
        deductions=c.execute("""SELECT COALESCE(SUM(p.amount_cents),0) FROM revenue_receipts r
          JOIN payment_evidence p ON p.id=r.payment_evidence_id JOIN revenue_orders o ON o.order_ref=r.order_ref
          WHERE p.currency=? AND p.authoritative=1 AND p.status='confirmed'
          AND p.evidence_kind IN ('refund','fee','expense')""",(currency,)).fetchone()[0]
        out[currency]=max(0,amount-int(deductions or 0))
    return out

def snapshot(c:sqlite3.Connection)->dict[str,Any]:
    return {"verified_available":verified_available_by_currency(c),
            "candidates":ranked(c,10),
            "allocations":[dict(r) for r in c.execute("""SELECT a.*,c.external_ref,c.lane_key,c.title,c.score
                FROM revenue_allocations a JOIN revenue_candidates c ON c.id=a.candidate_id
                ORDER BY a.points DESC,c.score DESC,c.id LIMIT 10""")],
            "waiting_capability":[dict(r) for r in c.execute("""SELECT c.id,c.external_ref,c.lane_key,c.title,w.required_action,w.capability_kind,w.blocker,c.score
                FROM revenue_candidates c JOIN revenue_capability_waits w ON w.candidate_id=c.id
                WHERE c.state='waiting_capability' ORDER BY c.score DESC,c.id LIMIT 20""")],
            "waiting_human":[dict(r) for r in c.execute("""SELECT id,external_ref,lane_key,title,human_gate,score
                FROM revenue_candidates WHERE state='waiting_human' ORDER BY score DESC,id LIMIT 20""")],
            "spending_authorized":False}

def control_cycle(c:sqlite3.Connection)->dict[str,Any]:
    reconcile_capability_waits(c)
    allocations=allocate(c)
    result=snapshot(c)
    from .revenue_continuation import reconcile
    result["continuation"] = reconcile(c)
    c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)",
              ("revenue.engine.cycle",_now(),json.dumps({"active_allocations":len(allocations),"verified_available":result["verified_available"]},sort_keys=True)))
    c.commit()
    return result
