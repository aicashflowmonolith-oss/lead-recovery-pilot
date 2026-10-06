"""Durable command -> plan -> route -> execute -> verify -> result.

The planner proposes only data. This module owns the operation allowlist.
No provider output can authorize network writes, spending, code or shell execution.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from . import ai_cli
from .queue import enqueue, get_state, initialize_queue

SCHEMA="""
CREATE TABLE IF NOT EXISTS execution_requests(
 id TEXT PRIMARY KEY, text TEXT NOT NULL, state TEXT NOT NULL,
 plan_json TEXT, provider TEXT, result TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS execution_requests_state ON execution_requests(state,updated_at);
CREATE TABLE IF NOT EXISTS execution_steps(
 request_id TEXT NOT NULL REFERENCES execution_requests(id), ordinal INTEGER NOT NULL,
 operation TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 result TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(request_id,ordinal));
CREATE TABLE IF NOT EXISTS execution_log(
 id INTEGER PRIMARY KEY, request_id TEXT NOT NULL REFERENCES execution_requests(id),
 state TEXT NOT NULL, detail TEXT NOT NULL, occurred_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS execution_log_request ON execution_log(request_id,id);
CREATE TABLE IF NOT EXISTS capability_build_tasks(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES execution_requests(id),
 capability TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 candidate TEXT NOT NULL DEFAULT '', routing_version TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
 engineering_run_id INTEGER, recipe_receipt_json TEXT NOT NULL DEFAULT '{}',
 recovery_generation INTEGER NOT NULL DEFAULT 0,
 UNIQUE(request_id,capability));
"""
ALLOWED={"answer","task.add","goal.add","note.add","monolith.calculate_roi",
         "monolith.measure_workflow","monolith.policy_review","capability.gap","artifact.write","artifact.test"}
QUICK=re.compile(r"^(task[: ]|goal[: ]|note[: ]|purchase[: ]|spent )",re.I)


def initialize(c):
    c.executescript(SCHEMA)
    if "routing_version" not in {r[1] for r in c.execute("PRAGMA table_info(capability_build_tasks)")}:
        c.execute("ALTER TABLE capability_build_tasks ADD COLUMN routing_version TEXT NOT NULL DEFAULT ''")
    columns = {r[1] for r in c.execute("PRAGMA table_info(capability_build_tasks)")}
    for name, declaration in (("engineering_run_id", "INTEGER"),
                              ("recipe_receipt_json", "TEXT NOT NULL DEFAULT '{}'"),
                              ("recovery_generation", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            c.execute(f"ALTER TABLE capability_build_tasks ADD COLUMN {name} {declaration}")
    initialize_queue(c)


def log(c,rid,state,detail):
    c.execute("UPDATE execution_requests SET state=?,updated_at=? WHERE id=?",(state,time.time(),rid))
    c.execute("INSERT INTO execution_log(request_id,state,detail,occurred_at) VALUES(?,?,?,?)",
              (rid,state,detail[:2000],time.time()))
    c.commit()


def submit(c,text,*,request_id=None):
    text=text.strip()
    if not 1<=len(text)<=1000:
        raise ValueError("Command must contain 1 to 1000 characters")
    rid=request_id or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}",rid):
        raise ValueError("Invalid request identifier")
    existing=c.execute("SELECT text FROM execution_requests WHERE id=?",(rid,)).fetchone()
    if existing and existing[0]!=text:
        raise ValueError("Request identifier reused with different text")
    c.execute("INSERT OR IGNORE INTO execution_requests(id,text,state,created_at,updated_at) VALUES(?,?,'queued',?,?)",
              (rid,text,time.time(),time.time()))
    # enqueue commits the request and queue record in the same SQLite transaction.
    enqueue(c,fingerprint="request:"+rid+":0",kind="request.execute",payload={"request_id":rid},priority=96,max_attempts=3)
    return rid


def complete_quick_command(c, rid):
    """Preserve immediate quick-add behavior while saving a durable request first."""
    from .queue import get_job
    row = c.execute("SELECT text FROM execution_requests WHERE id=?", (rid,)).fetchone()
    if not row or not QUICK.match(row["text"]) or stopped(c,rid):
        return None
    queued = c.execute("SELECT id FROM worker_jobs WHERE fingerprint=?", ("request:"+rid+":0",)).fetchone()
    home = Path(c.execute("PRAGMA database_list").fetchone()[2]).parent
    execute_request(c, get_job(c,queued[0]), home=home)
    return c.execute("SELECT result FROM execution_requests WHERE id=?", (rid,)).fetchone()[0]


def recent(c,limit=20):
    requests=[]
    for row in c.execute("SELECT * FROM execution_requests ORDER BY created_at DESC LIMIT ?",(limit,)):
        item=dict(row)
        item["steps"]=[dict(s) for s in c.execute("SELECT ordinal,operation,state,result,evidence_json FROM execution_steps WHERE request_id=? ORDER BY ordinal",(row["id"],))]
        item["gaps"]=[dict(g) for g in c.execute("SELECT * FROM capability_build_tasks WHERE request_id=?",(row["id"],))]
        item["logs"]=[dict(e) for e in c.execute("SELECT state,detail,occurred_at FROM execution_log WHERE request_id=? ORDER BY id DESC LIMIT 20",(row["id"],))]
        requests.append(item)
    return requests


def stopped(c,rid):
    return (any(get_state(c,key)=="1" for key in ("worker.paused","worker.emergency_stop","safe_mode.paused")) or
            c.execute("SELECT state FROM execution_requests WHERE id=?",(rid,)).fetchone()[0]=="cancelled")


class Interrupted(RuntimeError):
    pass


def cancel(c,rid):
    row=c.execute("SELECT state FROM execution_requests WHERE id=?",(rid,)).fetchone()
    if not row or row[0] in {"succeeded","cancelled"}:
        raise ValueError("Request is already complete or unavailable")
    log(c,rid,"cancelled","Cancelled by owner; completed local steps retained")


def supersede(c, rid, detail: str) -> bool:
    row=c.execute("SELECT state FROM execution_requests WHERE id=?",(rid,)).fetchone()
    if not row or row[0] in {"succeeded","cancelled"}:
        return False
    log(c,rid,"cancelled","Superseded by dead-letter reconciliation: "+detail[:1500])
    return True


def resume(c,rid):
    row=c.execute("SELECT state,generation FROM execution_requests WHERE id=?",(rid,)).fetchone()
    if not row or row[0] not in {"waiting_capability","failed","retry","cancelled"}:
        raise ValueError("Request cannot be resumed in its current state")
    generation=row[1]+1
    pending=c.execute("SELECT operation FROM execution_steps WHERE request_id=? AND state!='succeeded' ORDER BY ordinal LIMIT 1",(rid,)).fetchone()
    if pending and pending[0]=="capability.gap":
        # Replan only the unfinished suffix; retain every verified receipt.
        c.execute("DELETE FROM execution_steps WHERE request_id=? AND state!='succeeded'",(rid,))
        c.execute("UPDATE execution_requests SET plan_json=NULL WHERE id=?",(rid,))
    c.execute("UPDATE execution_requests SET state='queued',generation=?,error='',updated_at=? WHERE id=?",(generation,time.time(),rid))
    enqueue(c,fingerprint=f"request:{rid}:{generation}",kind="request.execute",payload={"request_id":rid},priority=96,max_attempts=3)


def validate_plan(value):
    if not isinstance(value,dict) or set(value)!={"summary","steps"}:
        raise ValueError("Invalid planner envelope")
    if not isinstance(value["summary"],str) or len(value["summary"])>4000:
        raise ValueError("Invalid planner summary")
    steps=value["steps"]
    if not isinstance(steps,list) or not 1<=len(steps)<=8:
        raise ValueError("Plan requires 1 to 8 steps")
    for ordinal,step in enumerate(steps):
        if not isinstance(step,dict) or set(step)!={"operation","payload"}:
            raise ValueError("Invalid plan step")
        if step["operation"] not in ALLOWED or not isinstance(step["payload"],str) or not 1<=len(step["payload"].strip())<=16000:
            raise ValueError("Invalid or unsupported plan operation")
        if step["operation"] == "artifact.test":
            data=json.loads(step["payload"])
            if not isinstance(data,dict) or set(data)!={"source_step","tests"} or type(data["source_step"]) is not int or not 0<=data["source_step"]<ordinal:
                raise ValueError("Tests require an earlier artifact step")
            if steps[data["source_step"]]["operation"] != "artifact.write":
                raise ValueError("Tests must reference an artifact")
            source=json.loads(steps[data["source_step"]]["payload"])
            if not source["filename"].endswith(".py") or not isinstance(data["tests"],str) or not 1<=len(data["tests"])<=12000:
                raise ValueError("Tests require Python source and bounded assertions")
            import ast
            if not any(isinstance(n,ast.Assert) for n in ast.walk(ast.parse(data["tests"]))):
                raise ValueError("Candidate tests require assertions")
        if step["operation"] == "artifact.write":
            validate_artifact(json.loads(step["payload"]))
        if step["operation"].startswith("monolith."):
            validate_business(step["operation"],json.loads(step["payload"]))
        if step["operation"] in {"task.add","goal.add"} and len(step["payload"])>1000:
            raise ValueError("Local record exceeds bounds")
    return value


def validate_business(op,data):
    import math
    fields={"monolith.calculate_roi":{"net_profit","investment","source_reference"},
            "monolith.measure_workflow":{"before_minutes","after_minutes","review_minutes","source_reference"},
            "monolith.policy_review":{"action"}}
    if op not in fields or not isinstance(data,dict) or set(data)!=fields[op]:
        raise ValueError("Unsupported governed operation or fields")
    for key,value in data.items():
        if key=="action":
            if value not in {"LOCAL_REVIEW_QUEUE","CUSTOMER_CONTACT","EXTERNAL_COMMUNICATION"}:
                raise ValueError("Unsupported policy action")
        elif key=="source_reference":
            if not isinstance(value,str) or not 1<=len(value)<=200:
                raise ValueError("Source reference required")
        elif type(value) not in (int,float) or not math.isfinite(value) or abs(value)>1e12 or (key!="net_profit" and value<0):
            raise ValueError("Invalid economic inputs")
    if op=="monolith.calculate_roi" and data["investment"]<=0:
        raise ValueError("Investment must be positive")


def validate_artifact(data):
    if not isinstance(data, dict) or set(data) != {"filename", "content"}:
        raise ValueError("Artifact requires filename and content")
    name = data["filename"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.(txt|md|json|py|js|html|css|csv)", name):
        raise ValueError("Artifact filename must be a plain supported filename")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *("COM"+str(n) for n in range(1,10)), *("LPT"+str(n) for n in range(1,10))}:
        raise ValueError("Reserved artifact name")
    if not isinstance(data["content"], str) or len(data["content"].encode()) > 64000:
        raise ValueError("Artifact content exceeds bounds")
    if name.endswith(".json"):
        json.loads(data["content"])
    if name.endswith(".py"):
        import ast
        ast.parse(data["content"])


def write_artifact(data, *, home, rid, ordinal):
    validate_artifact(data)
    directory = Path(home).resolve()/"execution"/"artifacts"/rid/str(ordinal)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/data["filename"]
    if not path.resolve().is_relative_to(Path(home).resolve()/"execution"/"artifacts"):
        raise ValueError("Artifact escaped request directory")
    content = data["content"].encode("utf-8")
    if path.exists() and path.read_bytes() != content:
        raise ValueError("Artifact exists with different contents")
    if not path.exists():
        with path.open("xb") as f:
            f.write(content)
    if path.read_bytes() != content:
        raise ValueError("Artifact verification failed")
    digest = hashlib.sha256(content).hexdigest()
    return "Created local artifact: " + str(path), {"path": str(path), "sha256": digest,
        "verification": "content read-back and syntax validation where supported", "code_executed": False}


class AtomicConnection:
    """Existing local APIs commit internally; defer those commits for atomic receipts."""
    def __init__(self,c): self.c=c
    def __getattr__(self,name): return getattr(self.c,name)
    def commit(self): pass


def routing_version():
    return hashlib.sha256(json.dumps(sorted(ALLOWED)).encode()).hexdigest()


def gap(c,rid,capability,reason):
    gid=hashlib.sha256((rid+capability).encode()).hexdigest()[:32]
    c.execute("INSERT OR IGNORE INTO capability_build_tasks(id,request_id,capability,reason,updated_at) VALUES(?,?,?,?,?)",
              (gid,rid,capability,reason[:2000],time.time()))
    c.execute("UPDATE capability_build_tasks SET routing_version=? WHERE id=?",(routing_version(),gid))
    from .local_capability_recipes import ARTIFACT_CAPABILITY
    if capability == ARTIFACT_CAPABILITY:
        previous = c.execute("SELECT state,recipe_receipt_json FROM capability_build_tasks WHERE id=?", (gid,)).fetchone()
        if previous["state"] in {"qualified", "resolved"}:
            c.execute("UPDATE capability_build_tasks SET state='pending',recipe_receipt_json=?,updated_at=? WHERE id=?",
                      (json.dumps({"qualified":False,"previous_qualification":json.loads(previous["recipe_receipt_json"])}),time.time(),gid))
    c.execute("UPDATE execution_requests SET error=? WHERE id=?",(reason[:2000],rid))
    log(c,rid,"waiting_capability",reason)
    if capability not in {"ai.authentication","monolith.local","sandbox.authorization"}:
        from .capability_factory import enqueue_gap
        enqueue_gap(c, gid)


def execute_local(c,rid,step):
    from .store import add_task,add_goal
    from .foundation import create_entity
    from .app import quick_add
    op=step["operation"]; payload=step["payload"]
    c.execute("BEGIN IMMEDIATE")
    try:
        saved = c.execute("SELECT state,result FROM execution_steps WHERE request_id=? AND ordinal=?", (rid,step["ordinal"])).fetchone()
        if saved and saved["state"] == "succeeded":
            c.commit()
            return saved["result"]
        if stopped(c,rid):
            raise Interrupted("Owner paused or cancelled execution")
        proxy=AtomicConnection(c)
        if op=="legacy.quick":
            result=quick_add(proxy,payload)
            evidence={"verification":"atomic legacy command and receipt", "request_id":rid}
        elif op=="answer":
            result=payload
            evidence={"verification":"model output received and schema validated", "fact_class":"ai_inferred"}
        elif op in {"task.add","goal.add"}:
            entity=(add_task if op=="task.add" else add_goal)(proxy,payload)
            table="tasks" if op=="task.add" else "goals"
            saved=c.execute("SELECT title FROM "+table+" WHERE id=?",(entity.id,)).fetchone()
            if not saved or saved[0]!=payload.strip():
                raise ValueError("Local record verification failed")
            result=f"Saved {table[:-1]} #{entity.id}: {payload}"
            evidence={"table":table,"id":entity.id,"verification":"read_after_write"}
        elif op=="note.add":
            entity=create_entity(proxy,entity_type="note",domain_key="learning",title=payload[:120],
                metadata={"text":payload},provenance={"request_id":rid,"source":"ai_plan","assessment_class":"ai_inferred"},fact_class="hypothesis",confidence=0.0)
            result="Note saved: "+payload
            evidence={"verification":"atomic note and receipt", "fact_class":"ai_inferred"}
        else:
            raise ValueError("Unknown local operation")
        c.execute("UPDATE execution_steps SET state='succeeded',result=?,evidence_json=? WHERE request_id=? AND ordinal=?",
                  (result,json.dumps(evidence),rid,step["ordinal"]))
        c.commit()
        return result
    except BaseException:
        c.rollback()
        raise


def execute_request(c,job,*,home,pulse=None,planner=None,business=None):
    rid=job.payload["request_id"]
    row=c.execute("SELECT * FROM execution_requests WHERE id=?",(rid,)).fetchone()
    if not row:
        raise ValueError("Unknown execution request")
    if row["state"] in {"succeeded","cancelled"}:
        return {"request_id":rid,"state":row["state"]}
    def check():
        if pulse: pulse()
        if stopped(c,rid): raise Interrupted("Owner paused or cancelled execution")
    try:
        check()
        if not row["plan_json"]:
            log(c,rid,"planning","Classifying request and selecting available capability")
            if QUICK.match(row["text"]):
                plan={"summary":"Explicit local command", "steps":[{"operation":"legacy.quick","payload":row["text"]}]}
                provider="life-os.deterministic"
            else:
                cap=ai_cli.select(c)
                provider=cap["name"]
                log(c,rid,"planning","Selected "+provider+" using deterministic capability routing")
                completed=[dict(s) for s in c.execute("SELECT operation,payload,result FROM execution_steps WHERE request_id=? AND state='succeeded' ORDER BY ordinal",(rid,))]
                prompt=row["text"]
                if completed:
                    prompt += "\nAlready completed for this request; do not repeat these operations: " + json.dumps([
                        {"operation":s["operation"],"payload":s["payload"][:1000],"result":s["result"][:1000]} for s in completed])
                plan=validate_plan((planner or ai_cli.plan)(cap,prompt,pulse=check))
            check()
            c.execute("UPDATE execution_requests SET plan_json=?,provider=? WHERE id=?",(json.dumps(plan),provider,rid))
            offset=c.execute("SELECT COALESCE(MAX(ordinal)+1,0) FROM execution_steps WHERE request_id=?",(rid,)).fetchone()[0]
            if offset+len(plan["steps"])>32:
                raise ValueError("Request exceeded its bounded step budget")
            for ordinal,step in enumerate(plan["steps"],start=offset):
                if step["operation"] == "artifact.test" and offset:
                    data=json.loads(step["payload"])
                    data["source_step"]+=offset
                    step={**step,"payload":json.dumps(data)}
                c.execute("INSERT OR IGNORE INTO execution_steps(request_id,ordinal,operation,payload) VALUES(?,?,?,?)",
                          (rid,ordinal,step["operation"],step["payload"]))
            c.commit()
        log(c,rid,"executing","Executing validated operations; completed steps are not replayed")
        for step in c.execute("SELECT * FROM execution_steps WHERE request_id=? ORDER BY ordinal",(rid,)).fetchall():
            check()
            if step["state"]=="succeeded": continue
            op=step["operation"]
            if op=="capability.gap":
                gap(c,rid,"unavailable.operation",step["payload"])
                return {"request_id":rid,"state":"waiting_capability"}
            if op == "artifact.write":
                from .local_capability_recipes import ARTIFACT_CAPABILITY, LocalCapabilityUnavailable, require_artifact_writer
                require_artifact_writer(c, home=home, pulse=check)
                try:
                    result,evidence=write_artifact(json.loads(step["payload"]),home=home,rid=rid,ordinal=step["ordinal"])
                except OSError:
                    from .capabilities import record_capability_failure
                    record_capability_failure(c, ARTIFACT_CAPABILITY, threshold=1,
                                              error="Local artifact I/O unavailable; fixed qualification required")
                    raise LocalCapabilityUnavailable("Local artifact I/O unavailable; original step retained") from None
                c.execute("UPDATE execution_steps SET state='succeeded',result=?,evidence_json=? WHERE request_id=? AND ordinal=?",
                          (result,json.dumps(evidence),rid,step["ordinal"]))
                c.commit()
            elif op == "artifact.test":
                from .sandbox_adapter import test_candidate
                result,evidence=test_candidate(json.loads(step["payload"]),home=home,rid=rid,pulse=check)
                c.execute("UPDATE execution_steps SET state='succeeded',result=?,evidence_json=? WHERE request_id=? AND ordinal=?",
                          (result,json.dumps(evidence),rid,step["ordinal"]))
                c.commit()
            elif op.startswith("monolith."):
                from .monolith_adapter import execute
                data=json.loads(step["payload"])
                validate_business(op,data)
                result,evidence=(business or execute)(op,data,request_id=rid,ordinal=step["ordinal"],home=home,pulse=check)
                check()
                c.execute("UPDATE execution_steps SET state='succeeded',result=?,evidence_json=? WHERE request_id=? AND ordinal=?",
                          (result,json.dumps(evidence),rid,step["ordinal"]))
                c.commit()
            else:
                result=execute_local(c,rid,step)
            log(c,rid,"executing",f"Step {step['ordinal']+1}: {op} verified")
        check()
        log(c,rid,"verifying","Checking persisted receipts for every planned step")
        if c.execute("SELECT COUNT(*) FROM execution_steps WHERE request_id=? AND state!='succeeded'",(rid,)).fetchone()[0]:
            raise ValueError("Not all steps verified")
        result="\n\n".join(s[0] for s in c.execute("SELECT result FROM execution_steps WHERE request_id=? ORDER BY ordinal",(rid,)))
        c.execute("UPDATE execution_requests SET result=?,error='' WHERE id=?",(result[:128000],rid))
        c.execute("UPDATE capability_build_tasks SET state='resolved',updated_at=? WHERE request_id=?",(time.time(),rid))
        log(c,rid,"succeeded","All planned operations have persisted verification receipts")
        return {"request_id":rid,"state":"succeeded"}
    except ai_cli.CapabilityUnavailable as exc:
        cap=getattr(exc,"capability",None) or ("sandbox.authorization" if "sandbox" in str(exc) else ("monolith.local" if "MONOLITH" in str(exc) else "ai.authentication"))
        gap(c,rid,cap,str(exc))
        return {"request_id":rid,"state":"waiting_capability"}
    except Interrupted:
        if c.execute("SELECT state FROM execution_requests WHERE id=?",(rid,)).fetchone()[0]!="cancelled":
            log(c,rid,"retry","Paused; completed steps retained")
        raise
    except Exception as exc:
        # Never persist model diagnostics or arbitrary subprocess output.
        error=(str(exc)[:300] if isinstance(exc,(ai_cli.AdapterError,TimeoutError)) else f"{type(exc).__name__}: execution failed; no unverified result accepted")
        c.execute("UPDATE execution_requests SET error=? WHERE id=?",(error,rid))
        log(c,rid,"failed" if job.attempts>=job.max_attempts else "retry",error)
        raise RuntimeError(error) from None


def build_capability(c,job,*,home=None,pulse=None,repo_root=None):
    from .capability_factory import process
    return process(c, job, home=home, pulse=pulse, repo_root=repo_root)


def recheck_waiting(c):
    """Resume missing capabilities when a new allowlisted handler is installed."""
    from .capability_factory import schedule_waiting
    schedule_waiting(c)
    changed=c.execute("SELECT DISTINCT r.id FROM execution_requests r JOIN capability_build_tasks g ON g.request_id=r.id WHERE r.state='waiting_capability' AND g.capability='unavailable.operation' AND g.routing_version!=? LIMIT 10",(routing_version(),)).fetchall()
    for row in changed: resume(c,row["id"])
    rows=c.execute("SELECT DISTINCT r.id FROM execution_requests r JOIN capability_build_tasks g ON g.request_id=r.id WHERE r.state='waiting_capability' AND g.capability='ai.authentication' LIMIT 10").fetchall()
    if rows:
        try: ai_cli.select(c)
        except ai_cli.CapabilityUnavailable: return
        for row in rows: resume(c,row["id"])


def recheck_sandbox(c,home):
    rows=c.execute("SELECT DISTINCT r.id FROM execution_requests r JOIN capability_build_tasks g ON g.request_id=r.id WHERE r.state='waiting_capability' AND g.capability='sandbox.authorization' LIMIT 10").fetchall()
    if rows:
        from .sandbox_adapter import probe
        try:
            if not probe(home): return
        except (OSError, TimeoutError, ai_cli.CapabilityUnavailable, ai_cli.AdapterError): return
        for row in rows: resume(c,row["id"])
