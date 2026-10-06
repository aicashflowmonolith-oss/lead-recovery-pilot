"""Universal capability registry and deterministic routing."""
from __future__ import annotations
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any
from .events import append_event

KINDS={"ai","api","browser","cli","local_app","database","cloud","device","public_data","automation","human_service","deterministic"}
PRIVACY_ORDER={"public":0,"internal":1,"personal":2,"sensitive":3}

def _now()->str:
    return datetime.now(timezone.utc).isoformat()

def _json(v:Any, default:Any)->str:
    return json.dumps(default if v is None else v,separators=(",",":"),sort_keys=True)

def upsert_capability(connection:sqlite3.Connection, *, name:str, kind:str, enabled:bool=True,
                      health:str="unknown", permissions:list[str]|None=None, auth_required:bool=False,
                      auth_status:str="unknown", cost_fixed_cents:int=0, privacy_class:str="public",
                      actions:list[str]|None=None, reversible:bool=True, rate_limit:dict[str,Any]|None=None,
                      failure_mode:str="", recovery_method:str="", owner_approval_required:bool=False,
                      priority:int=50, reliability:float=0.5, latency_ms:int=0,
                      metadata:dict[str,Any]|None=None)->int:
    if kind not in KINDS:
        raise ValueError("invalid capability kind")
    if privacy_class not in PRIVACY_ORDER:
        raise ValueError("invalid privacy class")
    connection.execute(
        """INSERT INTO capabilities(name,kind,enabled,health,permissions_json,auth_required,auth_status,
        cost_fixed_cents,privacy_class,actions_json,reversible,rate_limit_json,failure_mode,recovery_method,
        owner_approval_required,priority,reliability,latency_ms,metadata_json,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,enabled=excluded.enabled,health=excluded.health,
        permissions_json=excluded.permissions_json,auth_required=excluded.auth_required,auth_status=excluded.auth_status,
        cost_fixed_cents=excluded.cost_fixed_cents,privacy_class=excluded.privacy_class,
        actions_json=excluded.actions_json,reversible=excluded.reversible,rate_limit_json=excluded.rate_limit_json,
        failure_mode=excluded.failure_mode,recovery_method=excluded.recovery_method,
        owner_approval_required=excluded.owner_approval_required,priority=excluded.priority,
        reliability=excluded.reliability,latency_ms=excluded.latency_ms,metadata_json=excluded.metadata_json,
        updated_at=excluded.updated_at""",
        (name,kind,int(enabled),health,_json(permissions,[]),int(auth_required),auth_status,cost_fixed_cents,
         privacy_class,_json(actions,[]),int(reversible),_json(rate_limit,{}),failure_mode,recovery_method,
         int(owner_approval_required),priority,reliability,latency_ms,_json(metadata,{}),_now()),
    )
    connection.commit()
    row=connection.execute("SELECT id FROM capabilities WHERE name=?",(name,)).fetchone()
    return int(row["id"])

def set_capability_health(connection:sqlite3.Connection,name:str,health:str,metadata:dict[str,Any]|None=None)->bool:
    cur=connection.execute(
        "UPDATE capabilities SET health=?,metadata_json=CASE WHEN ? IS NULL THEN metadata_json ELSE ? END,updated_at=? WHERE name=?",
        (health,None if metadata is None else 1,_json(metadata,{}),_now(),name),
    )
    connection.commit()
    return cur.rowcount==1

def list_capabilities(connection:sqlite3.Connection)->list[dict[str,Any]]:
    rows=connection.execute("SELECT * FROM capabilities ORDER BY priority DESC,reliability DESC,name").fetchall()
    result=[]
    for r in rows:
        d=dict(r)
        for key in ("permissions_json","actions_json","rate_limit_json","metadata_json"):
            d[key[:-5] if key.endswith("_json") else key]=json.loads(d.pop(key))
        result.append(d)
    return result

def route_capabilities(connection:sqlite3.Connection, *, required_actions:list[str],
                       kind:str|None=None, max_cost_cents:int|None=None,
                       max_privacy_class:str="sensitive", allow_owner_approval:bool=False)->list[dict[str,Any]]:
    if max_privacy_class not in PRIVACY_ORDER:
        raise ValueError("invalid max privacy class")
    candidates=[]
    for cap in list_capabilities(connection):
        if not cap["enabled"] or cap["health"] not in {"healthy","degraded"}:
            continue
        circuit=cap["metadata"].get("circuit_breaker",{})
        reopen_at=circuit.get("reopen_at")
        if circuit.get("state")=="open" and (not reopen_at or reopen_at>_now()):
            continue
        if kind is not None and cap["kind"]!=kind:
            continue
        if not set(required_actions).issubset(set(cap["actions"])):
            continue
        if cap["auth_required"] and cap["auth_status"] != "ready":
            continue
        if max_cost_cents is not None and cap["cost_fixed_cents"]>max_cost_cents:
            continue
        if PRIVACY_ORDER.get(cap["privacy_class"],99)>PRIVACY_ORDER[max_privacy_class]:
            continue
        if cap["owner_approval_required"] and not allow_owner_approval:
            continue
        score=(cap["priority"]*10)+(cap["reliability"]*100)-min(cap["latency_ms"],10000)/100
        if cap["health"]=="degraded":
            score-=100
        d=dict(cap)
        d["route_score"]=round(score,3)
        d["provenance"]={"router":"life-os.capabilities.v1","required_actions":list(required_actions)}
        candidates.append(d)
    return sorted(candidates,key=lambda x:(-x["route_score"],x["name"]))

def probe_local_ai(connection:sqlite3.Connection)->list[dict[str,Any]]:
    specs=(("codex","codex"),("claude","claude"),("opencode","opencode"))
    out=[]
    for name,exe in specs:
        path=shutil.which(exe)
        health="unavailable"
        version=""
        if path:
            health="healthy"
            try:
                cp=subprocess.run([path,"--version"],capture_output=True,text=True,timeout=3,check=False)
                version=(cp.stdout or cp.stderr).strip().splitlines()[0][:200] if (cp.stdout or cp.stderr) else ""
                if cp.returncode!=0:
                    health="degraded"
            except Exception:
                health="degraded"
        existing = next((c for c in list_capabilities(connection) if c["name"] == f"ai.cli.{name}"), None)
        if existing and existing["metadata"].get("adapter") == "life_os.ai_cli.v1":
            out.append({"name":name,"available":bool(path),"health":existing["health"],"version":version})
            continue
        upsert_capability(
            connection,name=f"ai.cli.{name}",kind="ai",enabled=bool(path),health=health,
            permissions=["workspace"],auth_required=True,auth_status="unknown",privacy_class="internal",
            actions=["reasoning","coding","review"],reversible=True,owner_approval_required=False,
            priority={"codex":90,"claude":85,"opencode":80}[name],reliability=0.7,
            metadata={"executable_found":bool(path),"version":version},
        )
        out.append({"name":name,"available":bool(path),"health":health,"version":version})
    append_event(connection,"capabilities.local_ai_probed",{"providers":[x["name"] for x in out]})
    return out


def record_capability_failure(connection:sqlite3.Connection,name:str, *,
                              threshold:int=3,cooldown_seconds:int=300,error:str="")->bool:
    row=connection.execute("SELECT metadata_json FROM capabilities WHERE name=?",(name,)).fetchone()
    if row is None:
        return False
    metadata=json.loads(row["metadata_json"])
    cb=dict(metadata.get("circuit_breaker",{}))
    failures=int(cb.get("consecutive_failures",0))+1
    state="open" if failures>=max(1,threshold) else "closed"
    reopen_at=None
    if state=="open":
        reopen_at=(datetime.now(timezone.utc)+timedelta(seconds=max(1,cooldown_seconds))).isoformat()
    metadata["circuit_breaker"]={"state":state,"consecutive_failures":failures,
                                 "reopen_at":reopen_at,"last_error":error[:1000]}
    health="degraded" if state=="open" else "healthy"
    set_capability_health(connection,name,health,metadata)
    append_event(connection,"capability.failure",{"name":name,"state":state,"consecutive_failures":failures})
    return True

def record_capability_success(connection:sqlite3.Connection,name:str)->bool:
    row=connection.execute("SELECT metadata_json FROM capabilities WHERE name=?",(name,)).fetchone()
    if row is None:
        return False
    metadata=json.loads(row["metadata_json"])
    metadata["circuit_breaker"]={"state":"closed","consecutive_failures":0,"reopen_at":None,"last_error":""}
    set_capability_health(connection,name,"healthy",metadata)
    append_event(connection,"capability.recovered",{"name":name})
    return True
