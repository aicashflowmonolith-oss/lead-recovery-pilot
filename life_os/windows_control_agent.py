"""Native outbound Windows control agent for MONOLITH.

This is a capability-scoped control plane, not a remote shell. It polls one or
more MONOLITH-compatible endpoints, accepts only windows.control.v1 envelopes,
executes a fixed allowlist of locally-derived operations, and returns verified
receipts. No inbound socket is opened and no provider SDK is required.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import uuid

from .queue import get_state, set_state
from .worker import AlreadyRunningError, InstanceLock

CAPABILITY = "windows.control.v1"
AGENT_VERSION = 1
MAX_BYTES = 65536
MAX_FILE_BYTES = 48 * 1024
MAX_RECEIPT_BYTES = 48 * 1024
DEFAULT_ENDPOINTS = ("https://monolith-new-production.up.railway.app",)
POLL_SECONDS = 5.0
WORKER_STALE_SECONDS = 180.0
SELF_HEAL_COOLDOWN_SECONDS = 120.0
SELF_HEAL_KEY = "windows_control.last_worker_recovery_epoch"
PROCESS_TARGETS = {"worker","control_room","windows_control_agent","desktop_commander_guardian"}
TASK_PREFIXES = ("LIFE OS", "MONOLITH")


class ControlUnavailable(RuntimeError):
    pass


def _post(base: str, path: str, payload: dict[str, Any], token: str, timeout: float = 4.0) -> dict[str, Any]:
    parsed=urlparse(base)
    if parsed.scheme!="https" and not (parsed.scheme=="http" and parsed.hostname in {"127.0.0.1","localhost","::1"}):
        raise ValueError("control endpoint must use HTTPS")
    raw=json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    if len(raw)>MAX_BYTES: raise ValueError("control payload too large")
    req=Request(urljoin(base.rstrip("/")+"/",path.lstrip("/")),data=raw,headers={
        "Authorization":"Bearer "+token,"Content-Type":"application/json","Accept":"application/json"
    },method="POST")
    try:
        with urlopen(req,timeout=timeout) as response:
            data=response.read(MAX_BYTES+1)
    except HTTPError as exc:
        raise ControlUnavailable(f"HTTP {exc.code}") from None
    except (URLError,TimeoutError,OSError):
        raise ControlUnavailable("network unavailable") from None
    if len(data)>MAX_BYTES: raise ControlUnavailable("response too large")
    value=json.loads(data.decode())
    if not isinstance(value,dict): raise ControlUnavailable("invalid response")
    return value


def _machine_id(c: sqlite3.Connection) -> str:
    key="windows_control.machine_id"
    current=get_state(c,key)
    if isinstance(current,str) and current: return current
    value="windows-"+uuid.uuid4().hex
    set_state(c,key,value)
    return value


def _runtime_fingerprint() -> str:
    source=Path(__file__).resolve().read_bytes()
    return hashlib.sha256(str(AGENT_VERSION).encode()+b"\0"+source).hexdigest()


def _roots(home: Path, repo: Path) -> tuple[Path,...]:
    return (home.resolve(),repo.resolve())


def _bounded_path(raw: str, roots: tuple[Path,...]) -> Path:
    if not isinstance(raw,str) or not 1<=len(raw)<=1024: raise ValueError("invalid path")
    path=Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not any(path==root or path.is_relative_to(root) for root in roots):
        raise PermissionError("path outside allowed roots")
    return path


SENSITIVE_NAMES={".env","credentials","credentials.json","token","tokens","secrets","secrets.json","id_rsa","id_ed25519"}
SENSITIVE_SUFFIXES={".key",".pem",".pfx",".p12",".db",".sqlite",".sqlite3"}


def _safe_text_path(raw: str, roots: tuple[Path,...], *, writable: bool=False) -> Path:
    path=_bounded_path(raw,roots)
    relative_parts=[]
    for root in roots:
        if path==root or path.is_relative_to(root):
            relative_parts=[part.lower() for part in path.relative_to(root).parts]
            break
    if any(part==".git" for part in relative_parts):
        raise PermissionError("git metadata is not remotely readable or writable")
    name=path.name.lower()
    if name in SENSITIVE_NAMES or any(name.startswith(prefix+".") for prefix in ("credential","token","secret")):
        raise PermissionError("sensitive file is not remotely readable or writable")
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        raise PermissionError("sensitive file type is not remotely readable or writable")
    if writable:
        write_roots=(roots[0]/"control-artifacts", roots[0]/"runtime"/"native-control")
        if not any(path==root or path.is_relative_to(root) for root in write_roots):
            raise PermissionError("remote writes are limited to native-control data roots")
    return path


def _ps_literal(value: str) -> str:
    if not isinstance(value,str) or not 1<=len(value)<=200:
        raise ValueError("invalid PowerShell literal")
    return "'" + value.replace("'", "''") + "'"


def _ps_json(script: str, timeout: int = 20) -> Any:
    completed=subprocess.run(
        ["powershell.exe","-NoProfile","-NonInteractive","-Command",script],
        capture_output=True,text=True,timeout=timeout,check=False,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
    )
    if completed.returncode!=0: raise RuntimeError("bounded PowerShell operation failed")
    text=completed.stdout.strip()
    return json.loads(text) if text else None


def execute(operation: str, args: dict[str, Any], *, home: Path, repo: Path) -> dict[str, Any]:
    if not isinstance(args,dict) or len(json.dumps(args))>16000: raise ValueError("invalid args")
    roots=_roots(home,repo)

    if operation=="system.snapshot":
        return {
            "platform":sys.platform,
            "python":sys.version.split()[0],
            "pid":os.getpid(),
            "cwd":str(Path.cwd()),
            "home_exists":home.is_dir(),
            "repo_exists":repo.is_dir(),
        }

    if operation=="worker.recover":
        db=home/"life.db"
        backups=home/"backups"
        log_dir=home/"logs"
        backups.mkdir(parents=True,exist_ok=True)
        log_dir.mkdir(parents=True,exist_ok=True)
        argv=[
            sys.executable,"-m","life_os","--db",str(db),"worker",
            "--home",str(home),"--backups",str(backups),"--log",str(log_dir/"worker-native-recovery.log"),
        ]
        env=os.environ.copy()
        env["LIFE_OS_WORKER_LANE"]="all"
        env.pop("LIFE_OS_WORKER_START_GATE",None)
        env.pop("LIFE_OS_RECOVERY_SESSION",None)
        child=subprocess.Popen(
            argv,cwd=str(repo),env=env,stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,shell=False,
            close_fds=True,start_new_session=os.name!="nt",
            creationflags=(
                getattr(subprocess,"CREATE_NO_WINDOW",0)
                | getattr(subprocess,"DETACHED_PROCESS",0)
                | getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)
            ) if os.name=="nt" else 0,
        )
        return {"started":True,"pid":int(child.pid),"verification":"canonical worker process created"}

    if operation=="scheduled_task.run":
        name=args.get("name")
        if not isinstance(name,str) or not any(name.startswith(p) for p in TASK_PREFIXES):
            raise PermissionError("scheduled task outside MONOLITH namespace")
        script="$t=Get-ScheduledTask -TaskName "+_ps_literal(name)+" -ErrorAction Stop; Start-ScheduledTask -InputObject $t; @{name=$t.TaskName;state=$t.State.ToString()}|ConvertTo-Json -Compress"
        return {"task":_ps_json(script)}

    if operation=="scheduled_task.status":
        name=args.get("name")
        if not isinstance(name,str) or not any(name.startswith(p) for p in TASK_PREFIXES):
            raise PermissionError("scheduled task outside MONOLITH namespace")
        script="$t=Get-ScheduledTask -TaskName "+_ps_literal(name)+" -ErrorAction Stop; @{name=$t.TaskName;state=$t.State.ToString()}|ConvertTo-Json -Compress"
        return {"task":_ps_json(script)}

    if operation=="process.list":
        rows=_ps_json("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath | ConvertTo-Json -Compress")
        items=rows if isinstance(rows,list) else ([] if rows is None else [rows])
        return {"processes":items[:256],"truncated":len(items)>256}

    if operation=="process.start":
        target=args.get("target")
        if target not in PROCESS_TARGETS: raise PermissionError("process target not allowlisted")
        db=home/"life.db"
        log_dir=home/"logs"
        log_dir.mkdir(parents=True,exist_ok=True)
        targets={
            "worker":[sys.executable,"-m","life_os","--db",str(db),"worker","--home",str(home),"--backups",str(home/"backups"),"--log",str(log_dir/"worker.log")],
            "control_room":[sys.executable,"-m","life_os","--db",str(db),"app","--port","8766","--no-open"],
            "windows_control_agent":[sys.executable,"-m","life_os","--db",str(db),"windows-control","--home",str(home),"--repo-root",str(repo)],
            "desktop_commander_guardian":["powershell.exe","-NoProfile","-WindowStyle","Hidden","-ExecutionPolicy","Bypass","-File",str(repo/"scripts"/"desktop_commander_guardian.ps1"),"-HomeDir",str(home)],
        }
        argv=targets[target]
        child=subprocess.Popen(argv,cwd=str(repo),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,shell=False,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return {"started":True,"pid":int(child.pid),"target":target}
    if operation=="process.stop":
        pid=args.get("pid")
        if type(pid) is not int or pid<=0 or pid==os.getpid(): raise ValueError("invalid pid")
        repo_marker=str(repo).replace("'","''")
        home_marker=str(home).replace("'","''")
        script=(
            ("$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%d' -ErrorAction Stop; " % pid)
            + "$cmd=[string]$p.CommandLine; "
            + ("if (($cmd -notlike '*%s*') -and ($cmd -notlike '*%s*') -and ($cmd -notlike '*life_os*')) { throw 'process is not MONOLITH-owned' }; " % (repo_marker,home_marker))
            + ("Stop-Process -Id %d -Force -ErrorAction Stop; " % pid)
            + ("@{pid=%d;stopped=$true;name=$p.Name}|ConvertTo-Json -Compress" % pid)
        )
        return {"process":_ps_json(script)}

    if operation=="file.read":
        path=_safe_text_path(args.get("path",""),roots)
        if not path.is_file(): raise FileNotFoundError("file missing")
        data=path.read_bytes()
        if len(data)>MAX_FILE_BYTES: raise ValueError("file exceeds read bound")
        return {"path":str(path),"text":data.decode("utf-8"),"sha256":hashlib.sha256(data).hexdigest()}

    if operation=="file.write":
        path=_safe_text_path(args.get("path",""),roots,writable=True)
        text=args.get("text")
        expected=args.get("expected_sha256")
        if not isinstance(text,str) or len(text.encode())>MAX_FILE_BYTES: raise ValueError("file content outside bounds")
        if path.exists():
            current=hashlib.sha256(path.read_bytes()).hexdigest()
            if not isinstance(expected,str) or current!=expected:
                raise PermissionError("existing file requires matching expected_sha256")
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_name(path.name+".monolith.tmp")
        tmp.write_text(text,encoding="utf-8")
        os.replace(tmp,path)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        return {"path":str(path),"sha256":digest,"verified":True}

    raise ValueError("unsupported windows control operation")



def _initialize_receipts(c: sqlite3.Connection) -> None:
    c.execute("""
        CREATE TABLE IF NOT EXISTS windows_control_receipts (
            request_id TEXT PRIMARY KEY,
            request_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('executing','terminal')),
            outcome TEXT,
            result_json TEXT,
            observed_at TEXT NOT NULL
        )
    """)
    c.commit()


def _request_digest(payload: dict[str, Any]) -> str:
    raw=json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _reserve_or_replay(c: sqlite3.Connection, request_id: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    _initialize_receipts(c)
    digest=_request_digest(payload)
    c.execute("BEGIN IMMEDIATE")
    try:
        row=c.execute(
            "SELECT request_digest,state,outcome,result_json FROM windows_control_receipts WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO windows_control_receipts(request_id,request_digest,state,observed_at) VALUES(?,?,?,?)",
                (request_id,digest,"executing",datetime.now(timezone.utc).isoformat()),
            )
            c.commit()
            return "execute",None
        if row["request_digest"]!=digest:
            raise ValueError("request id replayed with different payload")
        if row["state"]=="terminal":
            result=json.loads(row["result_json"] or "{}")
            c.commit()
            return "terminal",{"outcome":row["outcome"],"result":result}
        result={"error":"interrupted_after_admission","verified":False,"reconciliation_required":True}
        encoded=json.dumps(result,sort_keys=True,separators=(",",":"))
        c.execute(
            "UPDATE windows_control_receipts SET state='terminal',outcome='failed',result_json=?,observed_at=? WHERE request_id=?",
            (encoded,datetime.now(timezone.utc).isoformat(),request_id),
        )
        c.commit()
        return "terminal",{"outcome":"failed","result":result}
    except BaseException:
        c.rollback()
        raise


def _finish_receipt(c: sqlite3.Connection, request_id: str, outcome: str, result: dict[str, Any]) -> None:
    encoded=json.dumps(result,sort_keys=True,separators=(",",":"),allow_nan=False)
    if len(encoded.encode())>MAX_RECEIPT_BYTES:
        result={"error":"result_exceeded_receipt_bound","verified":False}
        encoded=json.dumps(result,sort_keys=True,separators=(",",":"))
        outcome="failed"
    cursor=c.execute(
        "UPDATE windows_control_receipts SET state='terminal',outcome=?,result_json=?,observed_at=? WHERE request_id=? AND state='executing'",
        (outcome,encoded,datetime.now(timezone.utc).isoformat(),request_id),
    )
    if cursor.rowcount!=1:
        c.rollback()
        raise RuntimeError("control receipt reservation missing")
    c.commit()


def _state_dict(c: sqlite3.Connection, key: str) -> dict[str, Any]:
    raw=get_state(c,key)
    if not raw: return {}
    try: value=json.loads(raw)
    except (TypeError,json.JSONDecodeError): return {}
    return value if isinstance(value,dict) else {}


def _local_stopped(c: sqlite3.Connection) -> bool:
    return any(get_state(c,key)=="1" for key in ("worker.paused","worker.emergency_stop","safe_mode.paused"))


def _maybe_self_heal_worker(c: sqlite3.Connection, *, home: Path, repo: Path, now: float | None=None) -> dict[str, Any]:
    now=time.time() if now is None else float(now)
    if _local_stopped(c): return {"attempted":False,"reason":"locally_stopped"}
    heartbeat=_state_dict(c,"worker.heartbeat")
    stamp=heartbeat.get("timestamp_epoch")
    if type(stamp) in (int,float) and 0 <= now-float(stamp) <= WORKER_STALE_SECONDS:
        return {"attempted":False,"reason":"worker_fresh"}
    try: last=float(get_state(c,SELF_HEAL_KEY) or "0")
    except (TypeError,ValueError): last=0.0
    if last and now-last<SELF_HEAL_COOLDOWN_SECONDS:
        return {"attempted":False,"reason":"cooldown"}
    set_state(c,SELF_HEAL_KEY,str(now))
    try:
        result=execute("worker.recover",{},home=home,repo=repo)
    except Exception as exc:
        value={"attempted":True,"started":False,"reason":type(exc).__name__}
    else:
        value={"attempted":True,**result}
    set_state(c,"windows_control.last_worker_recovery",json.dumps(value,sort_keys=True))
    return value

def _validate_request(value: Any, machine_id: str) -> dict[str, Any]:
    if not isinstance(value,dict): raise ValueError("invalid request")
    if value.get("schema_version")!=1:
        raise ValueError("unsupported control schema")
    request_id=value.get("request_id")
    if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
        raise ValueError("invalid request id")
    expires=value.get("expires_at")
    if not isinstance(expires,str):
        raise ValueError("request expiry required")
    when=datetime.fromisoformat(expires)
    if when.tzinfo is None: when=when.replace(tzinfo=timezone.utc)
    if when.astimezone(timezone.utc)<=datetime.now(timezone.utc):
        raise ValueError("request expired")
    if value.get("capability")!=CAPABILITY or value.get("status")!="claimed" or value.get("claimed_by")!=machine_id:
        raise ValueError("request not claimable by this agent")
    payload=value.get("payload")
    if not isinstance(payload,dict) or set(payload)!={"operation","args","policy_ref"}:
        raise ValueError("invalid windows control payload")
    if not isinstance(payload["operation"],str) or len(payload["operation"])>80: raise ValueError("invalid operation")
    if not isinstance(payload["policy_ref"],str) or len(payload["policy_ref"])>200: raise ValueError("invalid policy ref")
    if not isinstance(payload["args"],dict): raise ValueError("invalid args")
    return value


def poll_once(c: sqlite3.Connection, *, home: Path, repo: Path, endpoints: tuple[str,...], token: str) -> dict[str, Any]:
    machine=_machine_id(c)
    heartbeat={
        "agent":"windows-control","agent_version":AGENT_VERSION,"runtime_fingerprint":_runtime_fingerprint(),
        "platform":sys.platform,"capabilities":[CAPABILITY],
    }
    last_error=None
    for base in endpoints:
        try:
            _post(base,"/control/heartbeat",{"client_id":machine,"payload":heartbeat},token)
            claimed=_post(base,"/control/claim",{"client_id":machine,"capabilities":[CAPABILITY]},token).get("request")
            if claimed is None:
                set_state(c,"windows_control.last_endpoint",base)
                return {"reachable":True,"claimed":False,"endpoint":base}
            request=_validate_request(claimed,machine)
            request_id=request["request_id"]
            checked=_post(base,"/control/check",{"client_id":machine,"request_id":request_id},token).get("request")
            checked=_validate_request(checked,machine)
            replay_state,cached=_reserve_or_replay(c,request_id,checked["payload"])
            if replay_state=="terminal":
                outcome=str(cached["outcome"])
                result=dict(cached["result"])
            else:
                try:
                    result=execute(checked["payload"]["operation"],checked["payload"]["args"],home=home,repo=repo)
                    outcome="succeeded"
                except Exception as exc:
                    result={"error":type(exc).__name__,"verified":False}
                    outcome="failed"
                _finish_receipt(c,request_id,outcome,result)
            _post(base,"/control/receipt",{"client_id":machine,"request_id":request_id,"outcome":outcome,"result":result},token)
            set_state(c,"windows_control.last_endpoint",base)
            set_state(c,"windows_control.last_receipt",json.dumps({"request_id":request_id,"outcome":outcome},sort_keys=True))
            return {"reachable":True,"claimed":True,"request_id":request_id,"outcome":outcome,"endpoint":base}
        except (ControlUnavailable,ValueError) as exc:
            last_error=type(exc).__name__
            continue
    return {"reachable":False,"reason":last_error or "no_endpoints"}


def run(c: sqlite3.Connection, *, home: Path, repo: Path, poll_seconds: float=POLL_SECONDS) -> int:
    raw=os.environ.get("LIFE_OS_WINDOWS_CONTROL_ENDPOINTS","").strip()
    endpoints=tuple(x.strip().rstrip("/") for x in raw.split(",") if x.strip()) or DEFAULT_ENDPOINTS
    try:
        with InstanceLock(home/"runtime"/"windows-control-agent.lock"):
            while True:
                recovery=_maybe_self_heal_worker(c,home=home,repo=repo)
                token=os.environ.get("LIFE_OS_CONTROL_BRIDGE_TOKEN","").strip()
                if 32<=len(token)<=512:
                    result=poll_once(c,home=home,repo=repo,endpoints=endpoints,token=token)
                else:
                    result={"reachable":False,"claimed":False,"reason":"credential_unconfigured"}
                result["worker_recovery"]=recovery
                result["observed_at_epoch"]=time.time()
                set_state(c,"windows_control.last_status",json.dumps(result,sort_keys=True))
                time.sleep(max(1.0,float(poll_seconds)))
    except AlreadyRunningError:
        return 0
