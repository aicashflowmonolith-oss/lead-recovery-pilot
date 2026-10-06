"""Persistent mission, resource balancing, learning, and safe scaling for LIFE OS.

This controller is deterministic. It does not spend money, send messages, accept
contracts, or bypass provider/account restrictions. It decides what deserves
attention and records why so the mission survives restarts and model changes.
"""
from __future__ import annotations
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

MISSION_KEY="sovereign-revenue-v1"
DEFAULT_MISSION="Maximize lawful verified spendable cash and durable long-term value while preserving safety, truth, liquidity, and user control."
OUTBOUND_IDENTITY="teagan.holland@outlook.com"

SCHEMA="""
CREATE TABLE IF NOT EXISTS sovereign_missions(
 key TEXT PRIMARY KEY, objective TEXT NOT NULL, success_metric TEXT NOT NULL,
 outbound_identity TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS brain_decisions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, work_ref TEXT NOT NULL,
 lane TEXT NOT NULL, priority REAL NOT NULL, reason TEXT NOT NULL,
 max_cost_cents INTEGER NOT NULL DEFAULT 0, occurred_at TEXT NOT NULL);
"""
SCHEMA += """
CREATE TABLE IF NOT EXISTS brain_outcomes(
 id INTEGER PRIMARY KEY AUTOINCREMENT, work_ref TEXT NOT NULL,
 outcome_kind TEXT NOT NULL, value REAL NOT NULL DEFAULT 0,
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
 evidence_ref TEXT NOT NULL DEFAULT '', occurred_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_brain_outcomes_work
ON brain_outcomes(work_ref,occurred_at DESC);
CREATE TABLE IF NOT EXISTS brain_policy_state(
 key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def _now()->str:
    return datetime.now(timezone.utc).isoformat()


def initialize(c:sqlite3.Connection)->None:
    c.executescript(SCHEMA)
    now=_now()
    c.execute("""INSERT OR IGNORE INTO sovereign_missions(
      key,objective,success_metric,outbound_identity,created_at,updated_at)
      VALUES(?,?,?,?,?,?)""",
      (MISSION_KEY,DEFAULT_MISSION,"authoritative settled/available cash",OUTBOUND_IDENTITY,now,now))
    c.commit()
def mission(c:sqlite3.Connection)->dict[str,Any]:
    initialize(c)
    row=c.execute("SELECT * FROM sovereign_missions WHERE key=?",(MISSION_KEY,)).fetchone()
    return dict(row)


def set_policy(c:sqlite3.Connection,key:str,value:Any)->None:
    if not isinstance(key,str) or not key.strip():
        raise ValueError("policy key required")
    body=json.dumps(value,separators=(",",":"),sort_keys=True)
    c.execute("""INSERT INTO brain_policy_state(key,value_json,updated_at) VALUES(?,?,?)
      ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
      (key.strip(),body,_now()))
    c.commit()


def get_policy(c:sqlite3.Connection,key:str,default:Any=None)->Any:
    row=c.execute("SELECT value_json FROM brain_policy_state WHERE key=?",(key,)).fetchone()
    return default if not row else json.loads(row[0])


def outbound_allowed(c:sqlite3.Connection,authenticated_sender:str)->bool:
    wanted=mission(c)["outbound_identity"].strip().lower()
    return isinstance(authenticated_sender,str) and authenticated_sender.strip().lower()==wanted


def record_authenticated_outbound_identity(c:sqlite3.Connection,authenticated_sender:str)->str:
    """Record provider-authenticated sender evidence; fail closed on any other identity."""
    allowed=outbound_allowed(c,authenticated_sender)
    set_policy(c,"outbound_identity_verified",allowed)
    if not allowed:
        raise ValueError("authenticated sender does not match the canonical commercial identity")
    return mission(c)["outbound_identity"]


def _unit(value:Any,name:str)->float:
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)


def classify_work(*,urgency:float,complexity:float,latency_sensitivity:float,
                  uncertainty:float,consequence:float)->str:
    urgency=_unit(urgency,"urgency"); complexity=_unit(complexity,"complexity")
    latency=_unit(latency_sensitivity,"latency_sensitivity")
    uncertainty=_unit(uncertainty,"uncertainty"); consequence=_unit(consequence,"consequence")
    deep_need=(complexity*0.35)+(uncertainty*0.35)+(consequence*0.30)
    fast_need=(urgency*0.45)+(latency*0.45)+((1-complexity)*0.10)
    if consequence>=0.8 or deep_need-fast_need>=0.15:
        return "deep"
    if fast_need-deep_need>=0.15:
        return "fast"
    return "balanced"


def choose_resource_budget(*,expected_value_cents:int,probability:float,max_fraction:float=.05)->int:
    if type(expected_value_cents) is not int or expected_value_cents<0:
        raise ValueError("expected_value_cents must be nonnegative whole cents")
    p=_unit(probability,"probability"); fraction=_unit(max_fraction,"max_fraction")
    return max(0,int(expected_value_cents*p*fraction))
def record_decision(c:sqlite3.Connection,work_ref:str,*,lane:str,priority:float,
                    reason:str,max_cost_cents:int=0)->int:
    if lane not in {"fast","balanced","deep"}: raise ValueError("invalid brain lane")
    if not isinstance(work_ref,str) or not work_ref.strip(): raise ValueError("work_ref required")
    if type(priority) not in (int,float) or not math.isfinite(priority): raise ValueError("priority required")
    if type(max_cost_cents) is not int or max_cost_cents<0: raise ValueError("invalid cost ceiling")
    cur=c.execute("""INSERT INTO brain_decisions(work_ref,lane,priority,reason,max_cost_cents,occurred_at)
      VALUES(?,?,?,?,?,?)""",(work_ref.strip(),lane,float(priority),reason[:2000],max_cost_cents,_now()))
    c.commit(); return int(cur.lastrowid)


def record_outcome(c:sqlite3.Connection,work_ref:str,outcome_kind:str,*,value:float=0,
                   verified:bool=False,evidence_ref:str="")->int:
    if not isinstance(work_ref,str) or not work_ref.strip(): raise ValueError("work_ref required")
    if not isinstance(outcome_kind,str) or not outcome_kind.strip(): raise ValueError("outcome_kind required")
    if type(value) not in (int,float) or not math.isfinite(value): raise ValueError("finite value required")
    if type(verified) is not bool: raise ValueError("verified must be boolean")
    cur=c.execute("""INSERT INTO brain_outcomes(work_ref,outcome_kind,value,verified,evidence_ref,occurred_at)
      VALUES(?,?,?,?,?,?)""",(work_ref.strip(),outcome_kind.strip(),float(value),int(verified),evidence_ref[:1000],_now()))
    c.commit(); return int(cur.lastrowid)


def learning_score(c:sqlite3.Connection,work_ref:str)->float:
    rows=c.execute("SELECT outcome_kind,value,verified FROM brain_outcomes WHERE work_ref=? ORDER BY id DESC LIMIT 50",(work_ref,)).fetchall()
    if not rows: return 1.0
    total=0.0; weight=0.0
    values={"verified_revenue":3.0,"sale":2.0,"buyer_signal":1.0,"reply":.25,
            "no_reply":-.1,"bounce":-.5,"complaint":-2.0,"refund":-2.0,"failure":-.75}
    for row in rows:
        base=values.get(row["outcome_kind"],0.0)
        if row["verified"]: base*=1.5
        total+=base; weight+=1.0
    return round(max(.2,min(3.0,1.0+(total/max(weight,1.0))*.25)),4)


def safe_scale_factor(c:sqlite3.Connection,work_ref:str)->float:
    """Scale only from verified evidence; unverified activity never earns >1x."""
    verified=c.execute("""SELECT outcome_kind,COUNT(*) n FROM brain_outcomes
      WHERE work_ref=? AND verified=1 GROUP BY outcome_kind""",(work_ref,)).fetchall()
    counts={r["outcome_kind"]:int(r["n"]) for r in verified}
    if counts.get("complaint") or counts.get("refund"):
        return .25
    if counts.get("verified_revenue",0)>=3:
        return 2.0
    if counts.get("verified_revenue",0)>=1:
        return 1.5
    if counts.get("buyer_signal",0)>=2:
        return 1.15
    return 1.0


def route_around_human_gates(c:sqlite3.Connection)->list[dict[str,Any]]:
    """Keep gated opportunities intact while directing autonomous work elsewhere."""
    executable=c.execute("SELECT id,external_ref,score FROM revenue_candidates WHERE state IN ('candidate','running') ORDER BY score DESC,id LIMIT 1").fetchone()
    if executable is None:
        return []
    gates=c.execute("SELECT id,external_ref,title,human_gate,score FROM revenue_candidates WHERE state='waiting_human' ORDER BY score DESC,id").fetchall()
    return [{'candidate_id':int(gate['id']),'external_ref':gate['external_ref'],
             'human_gate':gate['human_gate'],'preserved_state':'waiting_human',
             'routed_to':executable['external_ref']} for gate in gates]


def snapshot(c:sqlite3.Connection)->dict[str,Any]:
    initialize(c)
    recent=[dict(r) for r in c.execute("SELECT * FROM brain_decisions ORDER BY id DESC LIMIT 20")]
    return {"mission":mission(c),"recent_decisions":recent,
            "outbound_identity_required":OUTBOUND_IDENTITY,
            "outbound_identity_verified":bool(get_policy(c,"outbound_identity_verified",False)),
            "spending_authorized":False}
def control_cycle(c:sqlite3.Connection)->dict[str,Any]:
    initialize(c)
    routed_gates=route_around_human_gates(c)
    rows=c.execute("""SELECT external_ref,score,state,expected_net_profit_cents,probability,
      operational_burden,risk,evidence_confidence FROM revenue_candidates
      WHERE state IN ('candidate','running') ORDER BY score DESC,id LIMIT 20""").fetchall()
    decisions=[]
    for row in rows:
        learned=learning_score(c,row["external_ref"])
        scale=safe_scale_factor(c,row["external_ref"])
        priority=float(row["score"])*learned*scale
        consequence=min(1.0,float(row["risk"])+(.2 if row["expected_net_profit_cents"]>=100000 else 0))
        lane=classify_work(urgency=min(1.0,float(row["probability"])+.25),
            complexity=min(1.0,float(row["operational_burden"])+.2),latency_sensitivity=.7,
            uncertainty=max(0.0,1.0-float(row["evidence_confidence"])),consequence=consequence)
        budget=choose_resource_budget(expected_value_cents=int(row["expected_net_profit_cents"]),
            probability=float(row["probability"]),max_fraction=.02)
        record_decision(c,row["external_ref"],lane=lane,priority=priority,
            reason=f"revenue_score={row['score']}; learning={learned}; safe_scale={scale}",max_cost_cents=budget)
        decisions.append({"work_ref":row["external_ref"],"lane":lane,"priority":round(priority,4),"max_cost_cents":budget})
    from .events import append_event
    append_event(c,"brain.cycle",{"mission":MISSION_KEY,"decisions":len(decisions),"outbound_identity":OUTBOUND_IDENTITY})
    return {"mission":mission(c),"decisions":decisions,"routed_gates":routed_gates,"spending_authorized":False}
