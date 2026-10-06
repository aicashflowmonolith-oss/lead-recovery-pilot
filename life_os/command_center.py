"""Central Command Hub: native-first routing, schedules, self-healing and alerts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
import traceback as traceback_module
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .attention import request_approval
from .command_center_schema import initialize as initialize_schema
from .model_gateway import GatewayError, GatewayUnavailable, ModelGateway
from .principal_agent import AuthorityMode, PrincipalAgent
from .queue import Job, enqueue, get_state, initialize_queue, set_state
from . import request_fabric


REMIND_RE = re.compile(
    r"^remind me in\s+(\d+)\s*(seconds?|minutes?|hours?|days?)\s+(?:to\s+)?(.+)$",
    re.I,
)
TIMER_RE = re.compile(
    r"^timer\s+(\d+)\s*(seconds?|minutes?|hours?)(?:\s+(.+))?$",
    re.I,
)
EVERY_RE = re.compile(
    r"^every\s+(\d+)\s*(minutes?|hours?|days?)\s+(.+)$",
    re.I,
)
CRON_RE = re.compile(r"^cron:\s*([^|]+)\|\s*(.+)$", re.I)
QUICK_RE = re.compile(r"^(task[: ]|goal[: ]|note[: ]|purchase[: ]|spent )", re.I)

HIGH_RISK_TERMS = (
    " delete ", " remove ", " rm ", " format ", " transfer ", " pay ", " purchase ",
    " send email", " send message", " deploy ", " production ", " credential",
    " password", " api key", " secret", " git push", " merge ", " publish ",
)

SAFE_SHELL = {
    ("git", "status"),
    ("git", "diff", "--stat"),
    ("git", "log", "-1", "--oneline"),
    ("python", "--version"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value or {}, separators=(",", ":"), sort_keys=True, default=str)


def initialize(connection: sqlite3.Connection) -> None:
    initialize_schema(connection)
    initialize_queue(connection)


def log_activity(
    connection: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    level: str = "info",
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    initialize_schema(connection)
    cur = connection.execute(
        """INSERT INTO activity_logs(occurred_at,kind,level,message,task_id,metadata_json)
           VALUES(?,?,?,?,?,?)""",
        (_iso(), kind, level, message[:2000], task_id, _json(metadata)),
    )
    connection.commit()
    return int(cur.lastrowid)


def set_agent_state(
    connection: sqlite3.Connection,
    agent_key: str,
    status: str,
    *,
    task_id: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    initialize_schema(connection)
    connection.execute(
        """INSERT INTO agent_states(agent_key,status,current_task_id,state_json,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(agent_key) DO UPDATE SET
             status=excluded.status,current_task_id=excluded.current_task_id,
             state_json=excluded.state_json,updated_at=excluded.updated_at""",
        (agent_key, status, task_id, _json(state), _iso()),
    )
    connection.commit()


def risk_level(text: str) -> str:
    haystack = " " + text.lower() + " "
    return "high" if any(term in haystack for term in HIGH_RISK_TERMS) else "low"


def _seconds(value: int, unit: str) -> int:
    unit = unit.lower()
    if unit.startswith("second"):
        return value
    if unit.startswith("minute"):
        return value * 60
    if unit.startswith("hour"):
        return value * 3600
    if unit.startswith("day"):
        return value * 86400
    raise ValueError("Unsupported time unit")


def create_reminder(connection: sqlite3.Connection, message: str, remind_at: datetime) -> str:
    initialize_schema(connection)
    rid = uuid.uuid4().hex
    connection.execute(
        """INSERT INTO pending_reminders(id,message,remind_at,state,created_at)
           VALUES(?,?,?,'pending',?)""",
        (rid, message.strip()[:1000], _iso(remind_at), _iso()),
    )
    connection.commit()
    log_activity(
        connection, "reminder.created", message, task_id=rid,
        metadata={"remind_at": _iso(remind_at)},
    )
    return rid


def _cron_match(field: str, value: int, *, minimum: int, maximum: int) -> bool:
    field = field.strip()
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_match(part, value, minimum=minimum, maximum=maximum)
                   for part in field.split(","))
    number = int(field)
    if not minimum <= number <= maximum:
        raise ValueError("Cron field outside supported range")
    return value == number


def next_cron(expr: str, after: datetime | None = None) -> datetime:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must have five fields")
    current = (after or _now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = current + timedelta(days=366)
    while current <= limit:
        cron_weekday = (current.weekday() + 1) % 7
        if (
            _cron_match(parts[0], current.minute, minimum=0, maximum=59)
            and _cron_match(parts[1], current.hour, minimum=0, maximum=23)
            and _cron_match(parts[2], current.day, minimum=1, maximum=31)
            and _cron_match(parts[3], current.month, minimum=1, maximum=12)
            and (
                _cron_match(parts[4], cron_weekday, minimum=0, maximum=7)
                or (cron_weekday == 0 and parts[4].strip() == "7")
            )
        ):
            return current
        current += timedelta(minutes=1)
    raise ValueError("Cron expression did not produce a run within one year")


def create_scheduled_task(
    connection: sqlite3.Connection,
    *,
    name: str,
    directive: str,
    schedule_kind: str,
    schedule_value: str,
    next_run_at: datetime | None = None,
) -> str:
    initialize_schema(connection)
    name = name.strip()
    directive = directive.strip()
    if not 1 <= len(name) <= 200:
        raise ValueError("Scheduled task name must contain 1 to 200 characters")
    if not 1 <= len(directive) <= 4000:
        raise ValueError("Scheduled directive must contain 1 to 4000 characters")
    if schedule_kind not in {"once", "interval_seconds", "cron"}:
        raise ValueError("Unsupported schedule kind")
    if schedule_kind == "interval_seconds":
        interval = int(schedule_value)
        if interval < 60:
            raise ValueError("Scheduled intervals must be at least 60 seconds")
        next_run_at = next_run_at or (_now() + timedelta(seconds=interval))
    elif schedule_kind == "cron":
        next_run_at = next_run_at or next_cron(schedule_value)
    elif next_run_at is None:
        raise ValueError("One-time scheduled tasks require next_run_at")
    task_id = uuid.uuid4().hex
    now = _iso()
    connection.execute(
        """INSERT INTO scheduled_tasks(
             id,name,directive,schedule_kind,schedule_value,next_run_at,state,
             risk_level,retry_count,max_retries,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,'active',?,0,3,?,?)""",
        (
            task_id, name.strip()[:200], directive.strip()[:4000],
            schedule_kind, schedule_value, _iso(next_run_at),
            risk_level(directive), now, now,
        ),
    )
    connection.commit()
    log_activity(
        connection, "schedule.created", name, task_id=task_id,
        metadata={"schedule_kind": schedule_kind, "next_run_at": _iso(next_run_at)},
    )
    return task_id


def queue_command(connection: sqlite3.Connection, text: str, *, source: str = "dashboard") -> str:
    initialize(connection)
    text = text.strip()
    if not 1 <= len(text) <= 4000:
        raise ValueError("Command must contain 1 to 4000 characters")
    command_id = uuid.uuid4().hex
    enqueue(
        connection,
        fingerprint=f"command-center:directive:{command_id}",
        kind="command_center.directive",
        payload={"command_id": command_id, "text": text, "source": source},
        priority=98,
        max_attempts=3,
    )
    log_activity(
        connection, "command.queued", text[:300], task_id=command_id,
        metadata={"source": source, "risk": risk_level(text)},
    )
    return command_id


def _approval_for_shell(connection: sqlite3.Connection, command: str) -> tuple[bool, int]:
    fingerprint = "command-center:shell:" + hashlib.sha256(command.encode()).hexdigest()
    row = connection.execute(
        "SELECT id,state FROM approvals WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    if row and row["state"] == "approved":
        return True, int(row["id"])
    approval_id, _ = request_approval(
        connection,
        fingerprint=fingerprint,
        action="command_center.shell",
        risk="high",
        payload={"command": command[:1000]},
    )
    return False, approval_id


def _run_shell(connection: sqlite3.Connection, command: str, *, home: Path) -> str:
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError("Invalid shell command syntax") from exc
    if not args:
        raise ValueError("Shell command is empty")
    key = tuple(arg.lower() if i == 0 else arg for i, arg in enumerate(args))
    safe = key in SAFE_SHELL
    if not safe:
        approved, approval_id = _approval_for_shell(connection, command)
        if not approved:
            _send_alert({
                "kind": "high_risk_intercept",
                "action": "command_center.shell",
                "approval_id": approval_id,
            })
            log_activity(
                connection, "command.intercepted",
                "Shell command paused for owner approval",
                level="warning",
                metadata={"approval_id": approval_id},
            )
            return f"Paused for approval #{approval_id}"
    completed = subprocess.run(
        args,
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
        env=os.environ.copy(),
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode:
        raise RuntimeError(f"Shell command failed with exit {completed.returncode}: {output[:500]}")
    return output[:4000] or "Command completed with no output"


def _submit_preplanned(
    connection: sqlite3.Connection,
    text: str,
    plan: dict[str, Any],
    *,
    provider: str,
) -> str:
    request_fabric.initialize(connection)
    plan = request_fabric.validate_plan(plan)
    rid = uuid.uuid4().hex
    now = time.time()
    connection.execute(
        """INSERT INTO execution_requests(
             id,text,state,plan_json,provider,created_at,updated_at
           ) VALUES(?,?,'queued',?,?,?,?)""",
        (rid, text, json.dumps(plan), provider, now, now),
    )
    for ordinal, step in enumerate(plan["steps"]):
        connection.execute(
            """INSERT INTO execution_steps(request_id,ordinal,operation,payload)
               VALUES(?,?,?,?)""",
            (rid, ordinal, step["operation"], step["payload"]),
        )
    enqueue(
        connection,
        fingerprint=f"request:{rid}:0",
        kind="request.execute",
        payload={"request_id": rid},
        priority=96,
        max_attempts=3,
    )
    return rid


def _execute_native(connection: sqlite3.Connection, text: str, *, home: Path) -> str | None:
    lower = text.strip().lower()
    if lower == "reconcile engineering backlog":
        from .backlog_recovery import reconcile
        return json.dumps(reconcile(connection, home=home), sort_keys=True)
    if lower in {"status", "system status", "command center status"}:
        snap = snapshot(connection)
        return (
            f"{snap['counts']['scheduled_tasks']} scheduled task(s), "
            f"{snap['counts']['pending_reminders']} reminder(s), "
            f"{snap['counts']['open_errors']} open error(s)"
        )
    if lower in {"pause autonomous work", "pause worker"}:
        set_state(connection, "worker.paused", "1")
        return "Autonomous work paused after the current operation"
    if lower in {"resume autonomous work", "resume worker"}:
        set_state(connection, "worker.emergency_stop", "0")
        set_state(connection, "worker.paused", "0")
        return "Autonomous work resumed"

    match = REMIND_RE.match(text)
    if match:
        amount, unit, message = int(match.group(1)), match.group(2), match.group(3)
        when = _now() + timedelta(seconds=_seconds(amount, unit))
        rid = create_reminder(connection, message, when)
        return f"Reminder {rid[:8]} scheduled for {when.isoformat()}"

    match = TIMER_RE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        message = (match.group(3) or "Timer finished").strip()
        when = _now() + timedelta(seconds=_seconds(amount, unit))
        rid = create_reminder(connection, message, when)
        return f"Timer {rid[:8]} scheduled"

    match = EVERY_RE.match(text)
    if match:
        amount, unit, directive = int(match.group(1)), match.group(2), match.group(3)
        interval = _seconds(amount, unit)
        task_id = create_scheduled_task(
            connection,
            name=directive[:120],
            directive=directive,
            schedule_kind="interval_seconds",
            schedule_value=str(interval),
        )
        return f"Recurring task {task_id[:8]} scheduled every {interval} seconds"

    match = CRON_RE.match(text)
    if match:
        expr, directive = match.group(1).strip(), match.group(2).strip()
        task_id = create_scheduled_task(
            connection,
            name=directive[:120],
            directive=directive,
            schedule_kind="cron",
            schedule_value=expr,
        )
        return f"Cron task {task_id[:8]} scheduled"

    if lower.startswith("shell:"):
        return _run_shell(connection, text.split(":", 1)[1].strip(), home=home)

    if QUICK_RE.match(text):
        rid = request_fabric.submit(connection, text)
        result = request_fabric.complete_quick_command(connection, rid)
        return result or f"Request {rid[:8]} queued"

    return None


def execute_directive(
    connection: sqlite3.Connection,
    text: str,
    *,
    home: Path,
) -> dict[str, Any]:
    initialize(connection)

    principal = PrincipalAgent(connection)
    principal_decision = principal.decide(text)
    principal_meta = principal_decision.metadata()
    set_agent_state(
        connection,
        "teagan-principal",
        "evaluated",
        task_id=principal_decision.decision_id,
        state=principal_meta,
    )
    log_activity(
        connection,
        "principal.decision",
        f"{principal_decision.action_class}: {principal_decision.authority.value}",
        task_id=principal_decision.decision_id,
        metadata=principal_meta,
    )

    if principal_decision.authority is AuthorityMode.DENY:
        set_agent_state(
            connection,
            "teagan-principal",
            "denied",
            task_id=principal_decision.decision_id,
            state=principal_meta,
        )
        return {
            "route": "principal",
            "state": "denied",
            "decision_id": principal_decision.decision_id,
            "principal": principal_meta,
            "result": principal_decision.reason,
        }

    if principal_decision.authority is AuthorityMode.HUMAN:
        approval_id, _ = request_approval(
            connection,
            fingerprint=f"principal-human:{principal_decision.decision_id}",
            action=f"principal.{principal_decision.action_class.lower()}",
            risk=principal_decision.risk.value.lower(),
            payload={
                "decision_id": principal_decision.decision_id,
                "action_class": principal_decision.action_class,
                "reason": principal_decision.reason,
            },
        )
        set_agent_state(
            connection,
            "teagan-principal",
            "human_required",
            task_id=principal_decision.decision_id,
            state={**principal_meta, "approval_id": approval_id},
        )
        log_activity(
            connection,
            "principal.human_gate",
            principal_decision.reason,
            level="warning",
            task_id=principal_decision.decision_id,
            metadata={**principal_meta, "approval_id": approval_id},
        )
        return {
            "route": "principal",
            "state": "human_required",
            "decision_id": principal_decision.decision_id,
            "approval_id": approval_id,
            "principal": principal_meta,
            "result": principal_decision.reason,
        }

    native = _execute_native(connection, text, home=home)
    if native is not None:
        log_activity(
            connection,
            "command.completed",
            native,
            metadata={"route": "native", "principal": principal_meta},
        )
        return {"route": "native", "result": native, "principal": principal_meta}

    gateway = ModelGateway()
    if gateway.info.configured:
        plan = gateway.plan(text)
        rid = _submit_preplanned(
            connection, text, plan, provider=f"litellm:{gateway.info.model}"
        )
        log_activity(
            connection, "command.routed", f"Request {rid[:8]} routed through LiteLLM",
            metadata={"route": "litellm", "model": gateway.info.model},
        )
        return {"route": "litellm", "request_id": rid, "principal": principal_meta}

    rid = request_fabric.submit(connection, text)
    log_activity(
        connection, "command.routed",
        f"Request {rid[:8]} routed through existing capability fabric",
        metadata={"route": "capability_fabric"},
    )
    return {"route": "capability_fabric", "request_id": rid, "principal": principal_meta}


def execute_directive_job(
    connection: sqlite3.Connection,
    job: Job,
    *,
    home: Path,
) -> dict[str, Any]:
    command_id = str(job.payload["command_id"])
    text = str(job.payload["text"])
    set_agent_state(
        connection, "command-center", "running", task_id=command_id,
        state={"source": job.payload.get("source", "worker")},
    )
    try:
        result = execute_directive(connection, text, home=home)
        set_agent_state(
            connection, "command-center", "idle", state={"last_command_id": command_id}
        )
        return {"command_id": command_id, **result}
    except Exception:
        set_agent_state(
            connection, "command-center", "error", task_id=command_id,
            state={"last_error": traceback_module.format_exc(limit=1)[-1000:]},
        )
        raise


def _advance_task(row: sqlite3.Row) -> tuple[str, str]:
    if row["schedule_kind"] == "once":
        return "completed", row["next_run_at"]
    if row["schedule_kind"] == "interval_seconds":
        interval = int(row["schedule_value"])
        nxt = datetime.fromisoformat(row["next_run_at"])
        now = _now()
        while nxt <= now:
            nxt += timedelta(seconds=interval)
        return "active", _iso(nxt)
    if row["schedule_kind"] == "cron":
        return "active", _iso(next_cron(row["schedule_value"], _now()))
    raise ValueError("Unsupported schedule kind")


def execute_scheduled_job(
    connection: sqlite3.Connection,
    job: Job,
    *,
    home: Path,
) -> dict[str, Any]:
    task_id = str(job.payload["task_id"])
    initialize(connection)
    row = connection.execute(
        "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
    ).fetchone()
    if not row or row["state"] != "active":
        return {"task_id": task_id, "state": "inactive"}
    set_agent_state(
        connection, "scheduler", "running", task_id=task_id,
        state={"name": row["name"]},
    )
    result = execute_directive(connection, row["directive"], home=home)
    state, next_run = _advance_task(row)
    now = _iso()
    connection.execute(
        """UPDATE scheduled_tasks
           SET state=?,next_run_at=?,last_run_at=?,last_error='',
               retry_count=0,updated_at=? WHERE id=?""",
        (state, next_run, now, now, task_id),
    )
    connection.commit()
    log_activity(
        connection, "schedule.completed", row["name"], task_id=task_id,
        metadata={"state": state, "next_run_at": next_run, "route": result.get("route")},
    )
    set_agent_state(connection, "scheduler", "idle", state={"last_task_id": task_id})
    return {"task_id": task_id, "state": state, "result": result}


def _fire_due_reminders(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """SELECT id,message FROM pending_reminders
           WHERE state='pending' AND remind_at<=? ORDER BY remind_at,id""",
        (_iso(),),
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE pending_reminders SET state='fired',fired_at=? WHERE id=?",
            (_iso(), row["id"]),
        )
        log_activity(
            connection, "reminder.fired", row["message"],
            level="warning", task_id=row["id"],
        )
    connection.commit()
    return len(rows)


def schedule_due_jobs(connection: sqlite3.Connection) -> int:
    _fire_due_reminders(connection)
    rows = connection.execute(
        """SELECT id,next_run_at FROM scheduled_tasks
           WHERE state='active' AND next_run_at<=?
           ORDER BY next_run_at,id LIMIT 100""",
        (_iso(),),
    ).fetchall()
    created = 0
    for row in rows:
        _job, was_created = enqueue(
            connection,
            fingerprint=f"command-center:scheduled:{row['id']}:{row['next_run_at']}",
            kind="command_center.scheduled",
            payload={"task_id": row["id"], "scheduled_for": row["next_run_at"]},
            priority=94,
            max_attempts=3,
        )
        created += int(was_created)
    return created


def _send_alert(payload: dict[str, Any]) -> bool:
    endpoint = os.environ.get("LIFE_OS_ALERT_WEBHOOK_URL", "").strip()
    if not endpoint:
        return False
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "life-os/command-center"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def _repair_low_risk_directive(directive: str, error: str) -> str:
    normalized = " ".join(directive.split())
    gateway = ModelGateway()
    if gateway.info.configured:
        try:
            candidate = gateway.repair_instruction(normalized, error)
            if risk_level(candidate) == "low":
                return candidate
        except (GatewayUnavailable, GatewayError):
            pass
    return normalized


def record_job_failure(
    connection: sqlite3.Connection,
    job: Job,
    *,
    error: Exception,
    state: str,
    traceback_text: str = "",
) -> None:
    initialize(connection)
    task_id = str(job.payload.get("task_id") or job.payload.get("command_id") or "")
    severity = "critical" if state == "dead" else "error"
    error_id = uuid.uuid4().hex
    connection.execute(
        """INSERT INTO system_errors(
             id,task_id,occurred_at,severity,error_type,message,traceback,state,retryable
           ) VALUES(?,?,?,?,?,?,?,'open',?)""",
        (
            error_id, task_id or None, _iso(), severity, type(error).__name__,
            str(error)[:2000], traceback_text[-8000:], 0 if state == "dead" else 1,
        ),
    )
    if job.kind == "command_center.scheduled" and task_id:
        row = connection.execute(
            "SELECT directive,risk_level,retry_count FROM scheduled_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row:
            retry_count = int(row["retry_count"]) + 1
            directive = row["directive"]
            if state != "dead" and row["risk_level"] == "low" and retry_count <= 3:
                directive = _repair_low_risk_directive(directive, str(error))
            connection.execute(
                """UPDATE scheduled_tasks
                   SET directive=?,retry_count=?,last_error=?,updated_at=? WHERE id=?""",
                (directive, retry_count, str(error)[:2000], _iso(), task_id),
            )
    connection.commit()
    log_activity(
        connection, "system.error", f"{type(error).__name__}: {error}",
        level=severity, task_id=task_id or None,
        metadata={"worker_state": state, "job_kind": job.kind, "error_id": error_id},
    )
    if state == "dead":
        _send_alert({
            "kind": "critical_task_failure",
            "error_id": error_id,
            "task_id": task_id or None,
            "job_kind": job.kind,
        })


def snapshot(connection: sqlite3.Connection, *, limit: int = 50) -> dict[str, Any]:
    initialize_schema(connection)
    gateway = ModelGateway().info
    activity = [
        dict(row) for row in connection.execute(
            """SELECT id,occurred_at,kind,level,message,task_id,metadata_json
               FROM activity_logs ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
    ]
    scheduled = [
        dict(row) for row in connection.execute(
            """SELECT * FROM scheduled_tasks
               WHERE state IN ('active','paused')
               ORDER BY next_run_at,id LIMIT ?""",
            (limit,),
        )
    ]
    reminders = [
        dict(row) for row in connection.execute(
            """SELECT * FROM pending_reminders
               WHERE state='pending' ORDER BY remind_at,id LIMIT ?""",
            (limit,),
        )
    ]
    errors = [
        dict(row) for row in connection.execute(
            """SELECT id,task_id,occurred_at,severity,error_type,message,state,retryable
               FROM system_errors WHERE state='open'
               ORDER BY occurred_at DESC,id DESC LIMIT ?""",
            (limit,),
        )
    ]
    agents = [
        dict(row) for row in connection.execute(
            "SELECT * FROM agent_states ORDER BY agent_key"
        )
    ]
    heartbeat = get_state(connection, "worker.heartbeat")
    return {
        "activity": activity,
        "scheduled_tasks": scheduled,
        "pending_reminders": reminders,
        "system_errors": errors,
        "agent_states": agents,
        "worker": {
            "heartbeat": json.loads(heartbeat) if heartbeat else None,
            "paused": get_state(connection, "worker.paused") == "1",
            "emergency_stop": get_state(connection, "worker.emergency_stop") == "1",
        },
        "gateway": {
            "provider": gateway.provider,
            "configured": gateway.configured,
            "model": gateway.model,
        },
        "counts": {
            "scheduled_tasks": len(scheduled),
            "pending_reminders": len(reminders),
            "open_errors": len(errors),
            "critical_errors": sum(1 for row in errors if row["severity"] == "critical"),
            "agents": len(agents),
        },
    }
