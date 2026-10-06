"""Lightweight local-first LIFE OS daily dashboard."""
from __future__ import annotations

import html
import json
import logging
import re
import secrets
import sqlite3
import webbrowser
from contextlib import closing
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .attention import acknowledge_attention, decide_approval, list_approvals, list_attention
from .checkins import latest as latest_checkin, save_checkin
from .db import connect, initialize
from .context import build_context_packet, render_context, search_notes
from .finance import balances, transact
from .friction import record_owner_action, snapshot as friction_snapshot, start_observing
from .foundation import capture_unclassified, classify_unclassified, create_entity
from .goals import progress
from .money import list_payment_evidence, collection_snapshot
from .money_view import money_action, render_money
from .known_state import mark_reviewed
from .ontology import DOMAIN_REGISTRY
from .reality import awareness_snapshot
from .purchases import add_purchase, procurement_plan, queue as purchase_queue
from .queue import get_state, initialize_queue, set_state
from .reconcile import list_resources
from .service import LifeOS
from .store import add_goal, add_task, list_goals
from .sync import sync_status
from .worker import worker_status
from .command_center import queue_command, snapshot as command_center_snapshot
from .command_center_web import COMMAND_CENTER_JS, render as render_command_center
from . import request_fabric

MAX_FORM_BYTES = 32768
APP_MANIFEST = json.dumps({
    "name": "LIFE OS",
    "short_name": "LIFE OS",
    "description": "Local-first LIFE OS and MONOLITH control room",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f4f5f7",
    "theme_color": "#16181d",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
}, separators=(",", ":"))
APP_JS = r"""
const qs=(s)=>document.querySelector(s), qsa=(s)=>[...document.querySelectorAll(s)];
const setText=(id,value)=>{const el=document.getElementById(id);if(el&&value!==undefined&&value!==null)el.textContent=String(value)};
const formatEvent=(kind)=>String(kind||'activity').replaceAll('.',' › ').replaceAll('_',' ');
function renderActivity(items){const root=document.getElementById('live-activity');if(!root)return;root.replaceChildren(...(items||[]).slice(0,8).map(item=>{const row=document.createElement('div');row.className='activity-row';const dot=document.createElement('span');dot.className='activity-dot';const text=document.createElement('div');const strong=document.createElement('strong');strong.textContent=formatEvent(item.kind);const small=document.createElement('small');small.textContent=item.occurred_at||'';text.append(strong,small);row.append(dot,text);return row}))}
async function refreshLive(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)return;const s=await r.json(),a=s.awareness||{},mon=s.monolith||{},w=mon.worker||{},m=mon.mission||{},q=w.queue||{};setText('live-fresh',a.facts_fresh??0);setText('live-stale',a.facts_stale??0);setText('live-sources',(a.sources_healthy??0)+'/'+(a.sources_enabled??0));setText('live-gaps',a.requirements_gapped??0);setText('external-connected',a.external_connected??0);setText('external-unconfigured',a.external_unconfigured??0);setText('approval-count',(s.approvals||[]).length);setText('attention-count',(s.attention||[]).length);setText('queue-count',Object.values(q).reduce((n,v)=>n+(Number(v)||0),0));setText('mission-current',formatEvent(m.current_work||'Standing by'));setText('mission-running',m.running_jobs??0);setText('mission-engineering-open',m.engineering_open??0);setText('mission-queue',m.active_queue??0);setText('mission-throughput',m.completed_last_hour??0);setText('mission-lifetime',m.completed_total??0);setText('mission-providers',m.provider_summary||'Checking routes');setText('mission-revenue',m.verified_revenue_display||'$0 verified');setText('mission-revenue-count',m.verified_revenue_count??0);setText('mission-owner',m.owner_gate_count??0);setText('mission-healing',m.self_healing_label||'Health scan starting');setText('mission-failures',(m.failed_invariants||[]).length);setText('mission-repairs',(m.repairs_submitted||[]).length);setText('mission-heartbeat',m.heartbeat_fresh?'fresh':'stale or missing');setText('mission-waiting-provider',m.engineering_waiting_provider??0);setText('mission-memory',m.memory_pressure?'yes':'no');setText('mission-remote',m.remote_control_status||'checking');const missionState=document.getElementById('mission-state');if(missionState){missionState.dataset.state=m.status||'ready';missionState.textContent=m.status_label||'Ready'}const topState=document.getElementById('top-system-state');if(topState){topState.dataset.state=m.status||'ready';const label=topState.querySelector('[data-status-label]');if(label)label.textContent='MONOLITH '+String(m.status_label||'Ready').toLowerCase()}const pulse=document.getElementById('system-pulse');if(pulse){const alive=Boolean(m.heartbeat_fresh??w.heartbeat);pulse.dataset.state=alive?'live':'degraded';pulse.querySelector('span').textContent=alive?'Autonomy loop healthy':'Worker recovery needed'}renderActivity(s.activity||[]);refreshRequests(s.requests||[]);document.body.dataset.live='true'}catch(_){document.body.dataset.live='false'}}
let requestRevision='';async function refreshRequests(items){const revision=JSON.stringify(items);if(revision===requestRevision||document.querySelector('#request-results details[open]'))return;try{const r=await fetch('/requests',{cache:'no-store'});if(r.ok){const el=document.getElementById('request-results');if(el){el.innerHTML=await r.text();requestRevision=revision}}}catch(_){}}
function updateClock(){const el=document.getElementById('live-clock');if(el)el.textContent=new Intl.DateTimeFormat(undefined,{hour:'numeric',minute:'2-digit'}).format(new Date())}
window.addEventListener('load',()=>{if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js');updateClock();refreshLive();setInterval(updateClock,30000);setInterval(refreshLive,5000);qsa('[data-command]').forEach(b=>b.addEventListener('click',()=>{const input=qs('#universal-command');if(input){input.value=b.dataset.command||'';input.focus()}}));qsa('.danger-action').forEach(f=>f.addEventListener('submit',e=>{if(!confirm('Emergency stop autonomous LIFE OS work? Existing data will be preserved.'))e.preventDefault()}));qsa('a[href^="#"]').forEach(a=>a.addEventListener('click',()=>{const nav=qs('nav');if(nav)nav.classList.remove('open')}));});
"""
SERVICE_WORKER_JS = """self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));"""
ICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='#16181d'/><path d='M142 120h72v200h156v72H142z' fill='#f4f5f7'/><circle cx='336' cy='176' r='56' fill='#1f6feb'/></svg>"""


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}" + "$" + f"{abs(cents) / 100:,.2f}"


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _decode_worker_state(connection: sqlite3.Connection, key: str, default):
    raw = get_state(connection, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _engineering_provider_snapshot(connection: sqlite3.Connection) -> list[dict]:
    """Read provider truth without probing, logging in, or mutating capability state."""
    try:
        rows = connection.execute(
            """SELECT name,enabled,health,auth_required,auth_status,cost_fixed_cents,
                      owner_approval_required,priority,metadata_json
               FROM capabilities
               WHERE name LIKE 'engineering.cli.%'
               ORDER BY priority DESC,name"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    providers = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        circuit = metadata.get("circuit_breaker") or {}
        circuit_open = circuit.get("state") == "open"
        reopen_at = circuit.get("reopen_at")
        if circuit_open and reopen_at:
            try:
                circuit_open = datetime.fromisoformat(reopen_at) > now
            except (TypeError, ValueError):
                pass
        ready = (
            bool(row["enabled"])
            and row["health"] in {"healthy", "degraded"}
            and (not row["auth_required"] or row["auth_status"] == "ready")
            and not row["owner_approval_required"]
            and int(row["cost_fixed_cents"] or 0) == 0
            and not circuit_open
        )
        providers.append({
            "name": row["name"],
            "provider": str(metadata.get("provider") or row["name"].removeprefix("engineering.cli.")),
            "ready": ready,
            "health": row["health"],
            "auth_status": row["auth_status"],
            "circuit_open": circuit_open,
            "selected_model": str(metadata.get("selected_model") or ""),
        })
    return providers


def _mission_snapshot(
    connection: sqlite3.Connection,
    *,
    worker: dict,
    payments: list[dict],
    approvals: list[dict],
    attention: list[dict],
) -> dict:
    queue = worker.get("queue") or {}
    active_queue = sum(int(queue.get(state, 0) or 0) for state in ("queued", "retry", "running"))
    completed_total = int(queue.get("succeeded", 0) or 0)
    completed_last_hour = connection.execute(
        """SELECT COUNT(*) FROM worker_job_events
           WHERE kind='succeeded' AND julianday(occurred_at) >= julianday('now','-1 hour')"""
    ).fetchone()[0]
    current = connection.execute(
        """SELECT id,kind,state,updated_at FROM worker_jobs
           WHERE state='running' ORDER BY updated_at DESC,id DESC LIMIT 1"""
    ).fetchone()
    if current is None:
        current = connection.execute(
            """SELECT id,kind,state,updated_at FROM worker_jobs
               WHERE state IN ('queued','retry') ORDER BY priority DESC,available_at,id LIMIT 1"""
        ).fetchone()

    providers = _engineering_provider_snapshot(connection)
    ready_providers = [item for item in providers if item["ready"]]
    provider_names = [item["provider"] for item in ready_providers]

    operational = _decode_worker_state(connection, "operational.invariants", {})
    invariants = operational.get("invariants", {}) if isinstance(operational, dict) else {}
    failed_invariants = [key for key, value in invariants.items() if isinstance(value, dict) and not value.get("healthy")]
    repairs = operational.get("repairs", {}) if isinstance(operational, dict) else {}
    remote_invariant = invariants.get("windows_control.native", {}) if isinstance(invariants, dict) else {}
    remote_control_status = str(remote_invariant.get("status") or ("online" if remote_invariant.get("healthy") else "checking"))
    recovery = _decode_worker_state(connection, "recovery.status", {})
    paused = get_state(connection, "worker.paused") == "1"
    emergency = get_state(connection, "worker.emergency_stop") == "1"

    heartbeat = worker.get("heartbeat") if isinstance(worker.get("heartbeat"), dict) else None
    heartbeat_age = None
    heartbeat_fresh = False
    if heartbeat and isinstance(heartbeat.get("timestamp_epoch"), (int, float)):
        heartbeat_age = max(0.0, datetime.now(timezone.utc).timestamp() - float(heartbeat["timestamp_epoch"]))
        heartbeat_fresh = heartbeat_age <= 90

    engineering = worker.get("engineering") or {}
    engineering_open = sum(int(engineering.get(state, 0) or 0) for state in ("queued", "building", "verifying", "waiting_provider"))
    provider_gap = int(engineering.get("waiting_provider", 0) or 0) > 0 and not ready_providers
    owner_gate_count = len(approvals) + sum(1 for item in attention if item.get("kind") == "human_gate")

    totals: dict[str, int] = {}
    for item in payments:
        currency = str(item.get("currency") or "").upper()
        totals[currency] = totals.get(currency, 0) + int(item.get("amount_cents", 0) or 0)
    if not totals:
        revenue_display = "$0 verified"
    elif len(totals) == 1:
        currency, cents = next(iter(totals.items()))
        revenue_display = f"{_money(cents)} {currency}"
    else:
        revenue_display = f"{len(payments)} verified payments"

    memory_pressure = bool(recovery.get("memory_pressure")) if isinstance(recovery, dict) else False
    memory_load = recovery.get("memory_load_percent") if isinstance(recovery, dict) else None

    if emergency:
        status, status_label = "stopped", "Emergency stop"
    elif not heartbeat_fresh:
        status, status_label = "degraded", "Worker recovery needed"
    elif failed_invariants or provider_gap:
        status, status_label = "recovering", "Self-healing"
    elif owner_gate_count:
        status, status_label = "needs_you", "Needs you"
    elif paused:
        status, status_label = "paused", "Paused"
    elif active_queue:
        status, status_label = "working", "Working"
    else:
        status, status_label = "ready", "Ready"

    if failed_invariants:
        self_healing = f"Repairing {len(failed_invariants)} invariant{'s' if len(failed_invariants) != 1 else ''}"
    elif operational:
        self_healing = f"Watching {len(invariants)} invariants"
    else:
        self_healing = "Health scan starting"
    if memory_pressure:
        suffix = f" · RAM {memory_load}%" if memory_load is not None else " · RAM pressure"
        self_healing += suffix

    provider_summary = ", ".join(provider_names) if provider_names else "No engineering route ready"
    current_work = current["kind"] if current else ("Standing by" if active_queue == 0 else "Selecting next work")
    return {
        "status": status,
        "status_label": status_label,
        "heartbeat_fresh": heartbeat_fresh,
        "heartbeat_age_seconds": None if heartbeat_age is None else round(heartbeat_age, 1),
        "current_work": current_work,
        "active_queue": active_queue,
        "running_jobs": int(queue.get("running", 0) or 0),
        "completed_total": completed_total,
        "completed_last_hour": int(completed_last_hour),
        "providers": providers,
        "ready_providers": provider_names,
        "provider_summary": provider_summary,
        "provider_gap": provider_gap,
        "verified_revenue_display": revenue_display,
        "verified_revenue_count": len(payments),
        "owner_gate_count": owner_gate_count,
        "failed_invariants": failed_invariants,
        "repairs_submitted": sorted(repairs) if isinstance(repairs, dict) else [],
        "self_healing_label": self_healing,
        "remote_control_status": remote_control_status,
        "memory_pressure": memory_pressure,
        "memory_load_percent": memory_load,
        "engineering_open": engineering_open,
        "engineering_waiting_provider": int(engineering.get("waiting_provider", 0) or 0),
        "paused": paused,
        "emergency_stop": emergency,
    }


def app_snapshot(connection: sqlite3.Connection, *, full_requests: bool = True) -> dict:
    initialize_queue(connection)
    life = LifeOS(connection)
    now = life.now()
    today = life.today(480)
    accounts = balances(connection)
    live_cash_row = connection.execute(
        "SELECT value_json,source_key,observed_at,stale FROM reality_facts WHERE fact_key='money.current_cash'"
    ).fetchone()
    live_cash = None
    if live_cash_row is not None and not live_cash_row["stale"]:
        try:
            live_cash = int(json.loads(live_cash_row["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            live_cash = None
    fallback_cash = sum(a[2] for a in accounts)
    account_observed = []
    for account_id, _name, _balance in accounts:
        row = connection.execute(
            "SELECT metadata_json FROM canonical_entities WHERE id=?",
            (f"legacy:accounts:{account_id}",),
        ).fetchone()
        if row:
            try:
                observed = json.loads(row["metadata_json"]).get("balance_observed_at")
                if observed:
                    account_observed.append(observed)
            except json.JSONDecodeError:
                pass
    purchases = [dict(row) for row in purchase_queue(connection)]
    procurement = [dict(row) for row in procurement_plan(connection)]
    goals = []
    for goal in list_goals(connection):
        item = progress(connection, goal.id)
        item["priority"] = goal.priority
        item["status"] = goal.status
        goals.append(item)

    overdue = connection.execute(
        "SELECT id,title,due_date,priority FROM tasks "
        "WHERE status='open' AND due_date IS NOT NULL AND due_date < ? "
        "ORDER BY priority DESC,due_date,id",
        (date.today().isoformat(),),
    ).fetchall()

    failed_resources = list_resources(connection, status="failed", limit=25)
    unclassified = connection.execute(
        "SELECT COUNT(*) FROM unclassified_items WHERE state IN ('new','triaged')"
    ).fetchone()[0]
    approvals = list_approvals(connection, state="pending", limit=25)
    attention = list_attention(connection, open_only=True, limit=25)
    awareness = awareness_snapshot(connection)
    live_context: dict[str, dict] = {}
    for row in connection.execute(
        """SELECT fact_key,value_json,source_key,observed_at,stale
           FROM reality_facts
           WHERE fact_key IN ('system.device_identity','system.os','system.cpu_logical',
                              'system.memory_physical','system.home_disk_free','system.home_disk_total',
                              'lifeos.data.coverage','money.lifeos_accounts_snapshot')"""
    ):
        try:
            value = json.loads(row["value_json"])
        except json.JSONDecodeError:
            value = row["value_json"]
        live_context[row["fact_key"]] = {
            "value": value,
            "source_key": row["source_key"],
            "observed_at": row["observed_at"],
            "stale": bool(row["stale"]),
        }
    legacy_review_count = connection.execute(
        "SELECT COUNT(*) FROM known_state_imports WHERE review_required=1 AND review_state='pending'"
    ).fetchone()[0]
    payments = [
        item for item in list_payment_evidence(connection, limit=25)
        if item["authoritative"] and item["status"] == "confirmed" and item["evidence_kind"] == "payment"
    ]
    worker = worker_status(connection)
    mission = _mission_snapshot(
        connection, worker=worker, payments=payments, approvals=approvals, attention=attention
    )
    activity = [
        dict(row) for row in connection.execute(
            "SELECT id,kind,occurred_at,payload_json FROM events ORDER BY id DESC LIMIT 12"
        )
    ]
    risks = []
    if overdue:
        risks.append({"kind": "overdue_tasks", "label": f"{len(overdue)} overdue task(s)"})
    if failed_resources:
        risks.append({"kind": "failed_resources", "label": f"{len(failed_resources)} failed controller resource(s)"})
    if approvals:
        risks.append({"kind": "pending_approvals", "label": f"{len(approvals)} approval(s) waiting"})
    if unclassified:
        risks.append({"kind": "unclassified", "label": f"{unclassified} unclassified signal(s) to review"})

    return {
        "requests": request_fabric.recent(connection) if full_requests else [dict(r) for r in connection.execute("SELECT id,state,updated_at FROM execution_requests ORDER BY created_at DESC LIMIT 20")],
        "date": date.today().isoformat(),
        "next_action": None if now is None else {
            "id": now.task.id, "title": now.task.title,
            "minutes": now.task.effort_minutes, "score": now.score,
        },
        "today": [
            {"id": item.task.id, "title": item.task.title,
             "minutes": item.task.effort_minutes, "score": item.score,
             "due_date": item.task.due_date.isoformat() if item.task.due_date else None}
            for item in today
        ],

        "autonomy": friction_snapshot(connection),
        "attention": attention,
        "approvals": approvals,
        "accounts": [{"id": a[0], "name": a[1], "balance_cents": a[2]} for a in accounts],
        "net_cash_cents": live_cash if live_cash is not None else fallback_cash,
        "net_cash_live": live_cash is not None,
        "net_cash_source": live_cash_row["source_key"] if live_cash is not None else "legacy_accounts",
        "net_cash_observed_at": (
            live_cash_row["observed_at"] if live_cash is not None
            else (max(account_observed) if account_observed else None)
        ),
        "purchases": purchases,
        "procurement": procurement,
        "checkin": _row_dict(latest_checkin(connection)),
        "goals": goals,
        "monolith": {
            "worker": worker,
            "mission": mission,
            "verified_revenue": payments,
            "sync": sync_status(connection),
        },
        "risks": risks,
        "overdue": [dict(row) for row in overdue],
        "unclassified_count": unclassified,
        "awareness": awareness,
        "live_context": live_context,
        "legacy_review_count": legacy_review_count,
        "activity": activity,
        "unclassified": [
            dict(row) for row in connection.execute(
                """SELECT u.id,u.raw_kind,u.reason,u.created_at,e.title
                   FROM unclassified_items u
                   JOIN canonical_entities e ON e.id=u.entity_id
                   WHERE u.state IN ('new','triaged')
                   ORDER BY u.created_at DESC LIMIT 25"""
            )
        ],
        "domains": [
            dict(row) for row in connection.execute(
                """SELECT d.key,d.title,d.description,COUNT(e.id) AS entity_count
                   FROM life_domains d
                   LEFT JOIN canonical_entities e
                     ON e.domain_key=d.key AND e.archived_at IS NULL
                   WHERE d.active=1
                   GROUP BY d.key,d.title,d.description
                   ORDER BY d.title"""
            )
        ],
    }


def _parse_amount(text: str) -> tuple[int | None, str]:
    match = re.match(r"^\s*\$?([0-9]+(?:\.[0-9]{1,2})?)\s+(.+)$", text)
    if not match:
        return None, text.strip()
    return int(round(float(match.group(1)) * 100)), match.group(2).strip()


def quick_add(connection: sqlite3.Connection, text: str) -> str:
    raw = text.strip()
    if not raw:
        raise ValueError("Quick Add cannot be empty")
    lowered = raw.lower()

    if lowered.startswith("task:") or lowered.startswith("task "):
        title = raw.split(":", 1)[1].strip() if ":" in raw[:6] else raw[5:].strip()
        add_task(connection, title)
        return f"Task added: {title}"

    if lowered.startswith("goal:") or lowered.startswith("goal "):
        title = raw.split(":", 1)[1].strip() if ":" in raw[:6] else raw[5:].strip()
        add_goal(connection, title)
        return f"Goal added: {title}"

    if lowered.startswith("purchase:") or lowered.startswith("purchase "):
        rest = raw.split(":", 1)[1].strip() if ":" in raw[:10] else raw[9:].strip()
        cents, title = _parse_amount(rest)
        if cents is None:
            capture_unclassified(
                connection, title=raw[:120], raw_kind="quick_add",
                raw={"text": raw}, reason="purchase amount missing",
            )
            return "Captured for review because the purchase amount was unclear"
        add_purchase(connection, title, cents)
        return f"Purchase added: {title} ({_money(cents)})"

    if lowered.startswith("spent "):
        cents, category = _parse_amount(raw[6:])
        accounts = balances(connection)
        if cents is not None and len(accounts) == 1:
            transact(connection, accounts[0][0], -cents, category)
            return f"Expense recorded: {_money(cents)} for {category}"
        capture_unclassified(
            connection, title=raw[:120], raw_kind="quick_add",
            raw={"text": raw}, reason="expense needs one unambiguous account and amount",
        )
        return "Captured for review because the expense could not be routed safely"

    if lowered.startswith("note:") or lowered.startswith("note "):
        body = raw.split(":", 1)[1].strip() if ":" in raw[:6] else raw[5:].strip()
        create_entity(
            connection, entity_type="note", domain_key="learning",
            title=body[:120], metadata={"text": body}, provenance={"source": "quick_add"},
            fact_class="unknown", confidence=0.0,
        )
        return "Note saved"

    capture_unclassified(
        connection, title=raw[:120], raw_kind="quick_add",
        raw={"text": raw}, reason="no deterministic routing rule matched",
    )
    return "Captured safely in the unclassified inbox"


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _hidden(token: str, action: str) -> str:
    return (
        f"<input type='hidden' name='csrf' value='{_esc(token)}'>"
        f"<input type='hidden' name='action' value='{_esc(action)}'>"
    )


def _post_button(token: str, action: str, label: str, **fields: object) -> str:
    hidden = _hidden(token, action)
    hidden += "".join(
        f"<input type='hidden' name='{_esc(k)}' value='{_esc(v)}'>"
        for k, v in fields.items()
    )
    return f"<form method='post' action='/action' class='inline'>{hidden}<button>{_esc(label)}</button></form>"


def _request_rows(snapshot: dict, token: str) -> str:
    rows = []
    for item in snapshot.get("requests", []):
        actions = ""
        if item["state"] in {"waiting_capability", "failed", "retry", "cancelled"}:
            actions += _post_button(token, "request_resume", "Retry / recheck", request_id=item["id"])
        if item["state"] not in {"succeeded", "cancelled"}:
            actions += _post_button(token, "request_cancel", "Cancel", request_id=item["id"])
        for step in item["steps"]:
            if step["operation"] == "artifact.write" and step["state"] == "succeeded":
                actions += f"<a href='/artifact?request_id={_esc(item['id'])}&amp;step={step['ordinal']}'>Open saved artifact</a> "
        result = item["result"] or "\n\n".join(s["result"] for s in item["steps"] if s["result"])
        details = "".join(f"<p>{_esc(g['reason'])}</p><small>Capability task: {_esc(g['state'])}. Candidate drafts require sandbox tests and activation before use.</small><pre>{_esc(g['candidate'])}</pre>" for g in item["gaps"])
        logs = "".join(f"<li>{_esc(e['state'])}: {_esc(e['detail'])}</li>" for e in reversed(item["logs"]))
        operations = {step["operation"] for step in item["steps"]}
        label = item["state"]
        if label == "succeeded":
            label = "Answer generated" if operations == {"answer"} else ("Candidate tested" if "artifact.test" in operations else "Local steps completed")
        scope = ""
        if "answer" in operations:
            scope += "AI text is a draft or answer, not evidence that a sale, payment or delivery occurred. "
        if any(op.startswith("monolith.") for op in operations):
            scope += "Governed local calculation or policy review; no money moved. "
        if "artifact.test" in operations:
            scope += "Candidate assertions are not independent fulfillment verification or production activation."
        rows.append(f"<article class='item'><div><strong>{_esc(item['text'])}</strong>"
                    f"<small>{_esc(item['id'][:8])} · {_esc(label)} · {_esc(item['provider'] or 'waiting for route')}</small>"
                    f"<small>{_esc(scope)}</small><pre>{_esc(result)}</pre><small>{_esc(item['error'])}</small>{details}"
                    f"<details><summary>Execution history and provenance</summary><ul>{logs}</ul>"
                    f"<pre>{_esc(json.dumps(item['steps'], indent=2))}</pre></details>{actions}</div></article>")
    return "".join(rows) or "<p class='muted'>Commands and their verified results appear here.</p>"


def _task_rows(snapshot: dict, token: str) -> str:
    if not snapshot["today"]:
        return "<p class='muted'>Nothing queued.</p>"
    rows = []
    for item in snapshot["today"]:
        due = f" · due {_esc(item['due_date'])}" if item["due_date"] else ""
        done = _post_button(token, "task_done", "Done", task_id=item["id"])
        rows.append(
            f"<div class='item'><div><strong>{_esc(item['title'])}</strong>"
            f"<small>{item['minutes']}m · score {item['score']}{due}</small></div>{done}</div>"
        )
    return "".join(rows)


def _attention_rows(snapshot: dict, token: str) -> str:
    if not snapshot["attention"]:
        return "<p class='muted'>Nothing needs attention.</p>"
    rows = []
    for item in snapshot["attention"]:
        ack = _post_button(token, "attention_ack", "Acknowledge", attention_id=item["id"])
        rows.append(
            f"<div class='item'><div><strong>{_esc(item['kind'])}</strong>"
            f"<small>{_esc(item['severity'])} · {_esc(item['source'])}</small></div>{ack}</div>"
        )
    return "".join(rows)


def _approval_rows(snapshot: dict, token: str) -> str:
    if not snapshot["approvals"]:
        return "<p class='muted'>No approvals waiting.</p>"
    rows = []
    for item in snapshot["approvals"]:
        approve = _post_button(token, "approval_decide", "Approve", approval_id=item["id"], decision="approved")
        deny = _post_button(token, "approval_decide", "Deny", approval_id=item["id"], decision="denied")
        cost = _money(int(item["cost_cents"])) if item["cost_cents"] else "$0.00"
        detail = ""
        if item["fingerprint"].startswith("revenue-followthrough:"):
            payload = json.loads(item["payload_json"])
            proposal = payload.get("proposal", {})
            detail = (f"<p>{_esc(payload.get('summary', ''))}</p>"
                      f"<p>To: {_esc(proposal.get('recipient', ''))}<br>Subject: {_esc(proposal.get('subject', ''))}</p>"
                      f"<pre style='white-space:pre-wrap'>{_esc(proposal.get('body', ''))}</pre>"
                      f"<p>Evidence: {_esc(payload.get('evidence_url', ''))}</p>"
                      "<small>Connector interpretation; review against the evidence. Approval records your decision; sending still requires a separately authorized adapter.</small>")
        rows.append(
            f"<div class='item'><div><strong>{_esc(item['action'])}</strong>"
            f"<small>risk: {_esc(item['risk'])} · cost: {_esc(cost)}</small>{detail}</div>"
            f"<div class='actions'>{approve}{deny}</div></div>"
        )
    return "".join(rows)


def _autonomy_card(data: dict) -> str:
    observed = data.get("observed", {})
    revenue = data.get("revenue_followthrough") or {}
    started = data.get("observing_since") or "Worker has not established a baseline"
    return ("<section id='autonomy' class='card span-6'><h2>Less work for you</h2>"
            f"<p>Observed since {_esc(started)}. Counts cover this app only, over at most seven days.</p>"
            f"<p>Commands submitted: {observed.get('command', 0)} · Manual data operations: {observed.get('manual_operation', 0)} · Manual resumes: {observed.get('manual_resume', 0)}</p>"
            f"<p>Owner decisions: {observed.get('authorization', 0)} · Outcome reviews: {observed.get('outcome_review', 0)}</p>"
            f"<p>Revenue evidence: {_esc(revenue.get('state', 'awaiting worker'))}. Machine evidence requests: {data.get('machine_requests', 0)} · Completed reply assessments: {data.get('assessments_completed', 0)}</p>"
            "<p class='muted'>Time spent researching/checking, repeated context, required prompts, unnecessary escalations, and reminders outside this app are not measured yet. No time-saved or zero-effort claim is inferred from these counts.</p></section>")


def _render(snapshot: dict, token: str, message: str = "") -> str:
    next_action = snapshot["next_action"]
    now_html = (
        "<p class='muted'>No next action queued.</p>" if not next_action else
        f"<div class='now-title'>{_esc(next_action['title'])}</div>"
        f"<div class='muted'>{next_action['minutes']} minutes · score {next_action['score']}</div>"
        + _post_button(token, "task_done", "Mark done", task_id=next_action["id"])
    )
    cash_label = "Current cash" if snapshot["net_cash_live"] else "Last known cash — not live"
    cash_context = (
        f"Source: {_esc(snapshot['net_cash_source'])} · observed {_esc(snapshot['net_cash_observed_at'])}"
        if snapshot["net_cash_observed_at"]
        else f"Source: {_esc(snapshot['net_cash_source'])}"
    )
    money_rows = "".join(
        f"<div class='item'><strong>{_esc(a['name'])}</strong><span>{_esc(_money(a['balance_cents']))}</span></div>"
        for a in snapshot["accounts"]
    ) or "<p class='muted'>No accounts yet.</p>"
    purchase_rows = "".join(
        f"<div class='item'><div><strong>{_esc(p['title'])}</strong>"
        f"<small>{_esc(p['bucket'])} · ladder {p['ladder_rank']} · priority {p['priority']}"
        f"{' · necessary' if p['necessity'] else ''}</small></div>"
        f"<span>{_esc(_money(p['price_cents']))}</span></div>"
        for p in snapshot["purchases"]
    ) or "<p class='muted'>Purchase queue empty.</p>"
    active_procurement = next((m for m in snapshot["procurement"] if m["state"] == "active"), None)
    procurement_status = (
        "<p class='muted'>No active procurement milestone.</p>" if active_procurement is None else
        f"<p><strong>Current target:</strong> {_esc(active_procurement['title'])}</p>"
        f"<small>{_esc(active_procurement['bucket'])} · ladder {active_procurement['rank']}</small>"
    )

    goal_rows = "".join(
        f"<div class='item'><div><strong>{_esc(g['title'])}</strong>"
        f"<small>{g['done']}/{g['total']} tasks · priority {g['priority']}</small></div>"
        f"<span>{g['percent']}%</span></div>"
        for g in snapshot["goals"]
    ) or "<p class='muted'>No active goals yet.</p>"
    risk_rows = "".join(
        f"<div class='risk'>{_esc(r['label'])}</div>" for r in snapshot["risks"]
    ) or "<p class='muted'>No currently detected priority risks.</p>"
    check = snapshot["checkin"]
    health_summary = (
        "<p class='muted'>No check-in yet.</p>" if not check else
        f"<p>Sleep <strong>{_esc(check['sleep_hours'])}h</strong> · "
        f"Energy <strong>{_esc(check['energy'])}/10</strong> · "
        f"Mood <strong>{_esc(check['mood'])}/10</strong> · "
        f"Pain <strong>{_esc(check['pain'])}/10</strong></p>"
    )
    worker = snapshot["monolith"]["worker"]
    queue_state = worker.get("queue", {})
    flash = f"<div class='flash'>{_esc(message)}</div>" if message else ""
    csrf_quick = _hidden(token, "quick_add")
    csrf_task = _hidden(token, "task_add")
    csrf_goal = _hidden(token, "goal_add")
    csrf_purchase = _hidden(token, "purchase_add")
    csrf_checkin = _hidden(token, "checkin_save")
    csrf_account = _hidden(token, "account_add")
    csrf_tx = _hidden(token, "transaction_add")
    csrf_entity = _hidden(token, "entity_add")
    domain_options = "".join(
        f"<option value='{_esc(d['key'])}'>{_esc(d['title'])}</option>"
        for d in snapshot["domains"] if d["key"] != "unclassified"
    )
    domain_rows = "".join(
        f"<div class='item'><div><strong>{_esc(d['title'])}</strong>"
        f"<small>{_esc(d['description'])}</small></div><span>{d['entity_count']}</span></div>"
        for d in snapshot["domains"]
    )
    inbox_rows = []
    for item in snapshot["unclassified"]:
        classify = (
            f"<form method='post' action='/action' class='actions'>"
            f"{_hidden(token, 'classify_unclassified')}"
            f"<input type='hidden' name='item_id' value='{_esc(item['id'])}'>"
            f"<select name='domain_key' required>{domain_options}</select><button>Classify</button></form>"
        )
        inbox_rows.append(
            f"<div class='item'><div><strong>{_esc(item['title'])}</strong>"
            f"<small>{_esc(item['raw_kind'])} · {_esc(item['reason'])}</small></div>{classify}</div>"
        )
    inbox_html = "".join(inbox_rows) or "<p class='muted'>Unclassified inbox empty.</p>"
    awareness = snapshot["awareness"]
    source_rows = "".join(
        f"<div class='item'><div><strong>{_esc(s['title'])}</strong>"
        f"<small>{_esc(s['kind'])} · {_esc(s['health'])}"
        f"{' · active' if s['enabled'] else ' · not connected'}</small></div>"
        f"<span class='badge {'ok' if s['enabled'] and s['health']=='healthy' else 'warn'}'>{s['authority']} authority</span></div>"
        for s in awareness["sources"]
    )
    connection_rows = []
    for source in [s for s in awareness["sources"] if s["kind"] in {"bridge", "connector"}]:
        if source.get("live"):
            state, badge = "Connected and live", "ok"
        elif source["enabled"] and source["health"] == "healthy":
            state, badge = "Connected — last sync is stale", "warn"
        elif source["enabled"]:
            state, badge = "Connected — needs attention", "warn"
        else:
            state, badge = "Ready to connect — authorization or setup required", ""
        last = source["last_success_at"] or "No successful live sync yet"
        config = source.get("config") or {}
        purpose = config.get("purpose") or "Normalized live data source."
        domains = ", ".join(config.get("domains") or [])
        setup = config.get("setup") or "Register and authorize an adapter."
        connection_rows.append(
            f"<div class='connection-row'><div class='connection-main'><strong>{_esc(source['title'])}</strong>"
            f"<small>{_esc(purpose)}</small>"
            f"<small>{_esc(state)} · domains: {_esc(domains or 'general')} · last update: {_esc(last)}</small>"
            f"<small>{_esc(setup) if not source.get('live') else 'Live data is automatically reconciled into the canonical world model.'}</small></div>"
            f"<span class='badge {badge}'>{_esc(source['health'])}</span></div>"
        )
    connections_html = "".join(connection_rows) or "<p class='muted'>No external connectors are registered yet.</p>"
    device_fact = snapshot["live_context"].get("system.device_identity", {})
    device_value = device_fact.get("value") if isinstance(device_fact.get("value"), dict) else {}
    data_fact = snapshot["live_context"].get("lifeos.data.coverage", {})
    data_value = data_fact.get("value") if isinstance(data_fact.get("value"), dict) else {}
    account_fact = snapshot["live_context"].get("money.lifeos_accounts_snapshot", {})
    account_value = account_fact.get("value") if isinstance(account_fact.get("value"), dict) else {}
    device_summary = (
        f"{_esc(device_value.get('hostname') or 'This computer')} · "
        f"{_esc(device_value.get('system') or 'unknown OS')} · {_esc(device_value.get('machine') or 'unknown architecture')}"
    )
    memory_fact = snapshot["live_context"].get("system.memory_physical", {})
    memory_value = memory_fact.get("value") if isinstance(memory_fact.get("value"), dict) else {}
    disk_free_fact = snapshot["live_context"].get("system.home_disk_free", {})
    disk_total_fact = snapshot["live_context"].get("system.home_disk_total", {})
    memory_total_gb = round((memory_value.get("total_bytes", 0) or 0) / (1024 ** 3), 1)
    memory_available_gb = round((memory_value.get("available_bytes", 0) or 0) / (1024 ** 3), 1)
    disk_free_gb = round((disk_free_fact.get("value", 0) or 0) / (1024 ** 3), 1)
    disk_total_gb = round((disk_total_fact.get("value", 0) or 0) / (1024 ** 3), 1)
    device_telemetry = (
        f"RAM {memory_available_gb} GB available / {memory_total_gb} GB · "
        f"Disk {disk_free_gb} GB free / {disk_total_gb} GB"
        if memory_total_gb or disk_total_gb else "Telemetry will appear after the next reality scan."
    )
    knowledge_count = int(data_value.get("canonical_entities", 0) or 0)
    observation_count = int(data_value.get("observations", 0) or 0)
    local_account_count = len(account_value.get("accounts", [])) if isinstance(account_value.get("accounts"), list) else 0
    gap_rows = "".join(
        f"<div class='item'><div><strong>{_esc(g['title'])}</strong>"
        f"<small>{_esc(g['domain_key'])} · {_esc(g['reason'])} · source: {_esc(', '.join(g['preferred_sources']))}</small></div>"
        f"<span>{g['importance']}</span></div>"
        for g in awareness["gaps"]
    ) or "<p class='muted'>No required live facts are currently missing or stale.</p>"
    activity_rows = "".join(
        f"<div class='activity-row'><span class='activity-dot'></span><div>"
        f"<strong>{_esc(item['kind']).replace('.', ' › ')}</strong>"
        f"<small>{_esc(item['occurred_at'])}</small></div></div>"
        for item in snapshot["activity"][:8]
    ) or "<p class='muted'>No recent autonomous activity yet.</p>"
    queue_total = sum(int(value) for value in queue_state.values() if isinstance(value, (int, float)))
    worker_state_label = "Running" if worker.get("heartbeat") else "No heartbeat"
    pause_control = f"<form method='post' action='/action' class='inline'>{_hidden(token, 'worker_pause')}<button>Pause</button></form>"
    resume_control = f"<form method='post' action='/action' class='inline'>{_hidden(token, 'worker_resume')}<button>Resume</button></form>"
    emergency_control = f"<form method='post' action='/action' class='inline danger-action'>{_hidden(token, 'worker_emergency_stop')}<button class='danger'>Emergency stop</button></form>"

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='theme-color' content='#16181d'><meta name='apple-mobile-web-app-capable' content='yes'><meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>
<link rel='manifest' href='/manifest.webmanifest'><link rel='icon' href='/icon.svg' type='image/svg+xml'>
<title>LIFE OS</title><script src='/app.js' defer></script>
<style>
:root{{--bg:#071019;--panel:#0d1824;--panel2:#101f2e;--panel3:#132537;--line:rgba(164,196,224,.14);--line2:rgba(164,196,224,.24);--text:#f6f9fc;--muted:#91a5b8;--muted2:#60768a;--accent:#55b8ff;--accent2:#6ce5c3;--warn:#ffc96b;--danger:#ff717a;--shadow:0 18px 55px rgba(0,0,0,.28);--radius:18px;--fast:140ms;--normal:220ms}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;min-height:100vh;overflow-x:hidden;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:radial-gradient(circle at 78% -10%,rgba(85,184,255,.13),transparent 28rem),radial-gradient(circle at 35% 110%,rgba(108,229,195,.07),transparent 34rem),var(--bg);color:var(--text);letter-spacing:-.01em}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.014) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.014) 1px,transparent 1px);background-size:36px 36px;mask-image:linear-gradient(to bottom,black,transparent 72%)}}a{{color:inherit}}nav{{position:fixed;left:16px;top:16px;bottom:16px;width:218px;z-index:20;background:rgba(9,20,31,.82);backdrop-filter:blur(22px);border:1px solid var(--line);border-radius:22px;padding:20px 14px;box-shadow:var(--shadow);overflow:auto}}.brand{{display:flex;align-items:center;gap:11px;padding:4px 8px 20px;font-size:20px;font-weight:820;letter-spacing:.08em}}.brand-mark{{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:linear-gradient(145deg,rgba(85,184,255,.26),rgba(108,229,195,.11));border:1px solid rgba(85,184,255,.36);box-shadow:inset 0 0 22px rgba(85,184,255,.1)}}.nav-section{{margin:10px 8px 6px;color:var(--muted2);font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}}nav a{{display:flex;align-items:center;gap:10px;text-decoration:none;color:#b9c7d5;padding:9px 10px;border-radius:11px;margin:2px 0;font-size:13px;transition:background var(--fast),color var(--fast),transform var(--fast)}}nav a:hover,nav a:focus-visible{{background:rgba(85,184,255,.1);color:#fff;transform:translateX(2px)}}.nav-dot{{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.48}}.nav-footer{{margin-top:22px;padding:13px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.025)}}.tiny{{font-size:11px;color:var(--muted)}}main{{margin-left:250px;padding:18px 24px 48px;max-width:1580px}}.topbar{{display:flex;align-items:center;justify-content:space-between;gap:14px;min-height:54px;margin-bottom:18px}}.system-pill{{display:inline-flex;align-items:center;gap:8px;padding:8px 11px;border:1px solid var(--line);border-radius:999px;background:rgba(12,25,38,.72);font-size:12px;color:#c8d5e1}}.live-dot{{width:8px;height:8px;border-radius:50%;background:var(--accent2);box-shadow:0 0 0 0 rgba(108,229,195,.45);animation:pulse 2.4s ease-out infinite}}#system-pulse[data-state='degraded'] .live-dot{{background:var(--warn)}}.clock{{font-variant-numeric:tabular-nums;color:var(--muted);font-size:13px}}.hero{{padding:28px 0 20px;max-width:1050px}}.eyebrow{{font-size:11px;color:var(--accent2);font-weight:800;letter-spacing:.16em;text-transform:uppercase;margin-bottom:9px}}.hero h1{{font-size:clamp(30px,4.8vw,56px);line-height:1.02;margin:0 0 10px;letter-spacing:-.05em}}.hero p{{margin:0;color:var(--muted);max-width:720px;line-height:1.55}}.command-shell{{display:flex;align-items:center;gap:10px;margin-top:22px;padding:9px 9px 9px 16px;border-radius:17px;background:linear-gradient(180deg,rgba(18,37,55,.96),rgba(12,26,40,.96));border:1px solid rgba(85,184,255,.36);box-shadow:0 0 0 1px rgba(85,184,255,.04),0 18px 45px rgba(0,0,0,.24)}}.command-shell:focus-within{{border-color:rgba(85,184,255,.78);box-shadow:0 0 0 3px rgba(85,184,255,.09),0 18px 45px rgba(0,0,0,.24)}}.command-shell input{{flex:1;border:0!important;background:transparent!important;color:var(--text)!important;padding:11px 4px!important;font-size:16px!important;outline:none}}.command-shell button{{min-width:48px;height:44px;border-radius:12px;background:linear-gradient(135deg,#4dafff,#5fd9c5);border:0;color:#071019;font-size:20px;font-weight:900;box-shadow:0 8px 24px rgba(85,184,255,.18)}}.command-hints{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}.hint{{border:1px solid var(--line);background:rgba(255,255,255,.025);color:#aebed0;border-radius:999px;padding:7px 10px;font-size:12px;cursor:pointer;transition:transform var(--fast),border-color var(--fast),background var(--fast)}}.hint:hover{{transform:translateY(-1px);border-color:var(--line2);background:rgba(85,184,255,.065)}}.grid{{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px;align-items:start}}.card{{position:relative;overflow:hidden;grid-column:span 4;background:linear-gradient(180deg,rgba(16,31,46,.88),rgba(11,24,36,.92));border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:0 14px 35px rgba(0,0,0,.14);transition:transform var(--normal),border-color var(--normal),box-shadow var(--normal)}}.card:after{{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(120deg,rgba(255,255,255,.025),transparent 35%)}}.card:hover{{transform:translateY(-2px);border-color:var(--line2);box-shadow:0 20px 44px rgba(0,0,0,.2)}}.card h2{{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:14px;letter-spacing:.015em;margin:0 0 14px;color:#dce6ef}}.card h3{{font-size:12px;color:var(--muted);margin:18px 0 8px;text-transform:uppercase;letter-spacing:.08em}}.span-8{{grid-column:span 8}}.span-6{{grid-column:span 6}}.wide{{grid-column:1/-1}}.now{{min-height:230px;background:radial-gradient(circle at 90% 10%,rgba(85,184,255,.16),transparent 18rem),linear-gradient(145deg,rgba(17,37,58,.98),rgba(11,24,36,.96))}}.now-title{{font-size:clamp(23px,3vw,35px);font-weight:790;line-height:1.08;letter-spacing:-.035em;margin:24px 0 8px;max-width:680px}}.section-kicker{{color:var(--accent2);font-size:11px;text-transform:uppercase;letter-spacing:.13em;font-weight:800}}.metric-row{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}}.metric-tile{{background:rgba(255,255,255,.025);border:1px solid var(--line);border-radius:13px;padding:13px;min-height:88px}}.stat{{font-size:26px;font-weight:820;letter-spacing:-.04em;font-variant-numeric:tabular-nums}}.label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.09em;margin-top:5px}}.item{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 0;border-top:1px solid rgba(164,196,224,.1)}}.item:first-child{{border-top:0}}.item strong{{font-size:13px}}small,.muted{{display:block;color:var(--muted);margin-top:4px;font-size:11px;line-height:1.45}}.actions,.inline{{display:flex;gap:7px;margin:0;align-items:center}}button{{font:inherit;border:1px solid var(--line2);background:rgba(255,255,255,.04);color:#dce8f3;border-radius:10px;padding:8px 11px;cursor:pointer;transition:transform var(--fast),background var(--fast),border-color var(--fast),box-shadow var(--fast)}}button:hover{{background:rgba(85,184,255,.09);border-color:rgba(85,184,255,.32);transform:translateY(-1px)}}button:active{{transform:translateY(0) scale(.98)}}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{{outline:3px solid rgba(85,184,255,.35);outline-offset:2px}}.primary{{background:linear-gradient(135deg,rgba(85,184,255,.95),rgba(108,229,195,.9));color:#06111a;border:0;font-weight:800}}.danger{{color:#ffd8db;border-color:rgba(255,113,122,.35);background:rgba(255,113,122,.08)}}input,select,textarea{{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 10px;background:rgba(3,12,20,.55);color:var(--text);font:inherit}}select option{{background:#0e1a26}}textarea{{min-height:72px;resize:vertical}}.form-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}}.form-grid button{{width:100%}}.flash{{position:sticky;top:12px;z-index:30;background:rgba(20,72,61,.96);border:1px solid rgba(108,229,195,.35);padding:11px 14px;border-radius:12px;margin-bottom:12px;box-shadow:var(--shadow)}}.risk{{padding:9px 10px;margin:7px 0;background:rgba(255,201,107,.07);border:1px solid rgba(255,201,107,.16);border-left:3px solid var(--warn);border-radius:9px;color:#efdcb9}}.status-line{{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}}.control-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}.activity-list{{display:grid;gap:1px}}.activity-row{{display:grid;grid-template-columns:10px 1fr;gap:10px;align-items:start;padding:9px 0;border-top:1px solid rgba(164,196,224,.09)}}.activity-row:first-child{{border-top:0}}.activity-dot{{width:7px;height:7px;margin-top:5px;border-radius:50%;background:var(--accent);box-shadow:0 0 14px rgba(85,184,255,.55)}}.activity-row strong{{display:block;font-size:12px;font-weight:650}}details{{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}}summary{{cursor:pointer;color:#c9d5e1;font-size:12px}}.domain-scroll{{max-height:390px;overflow:auto;padding-right:3px}}.badge{{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 8px;border:1px solid var(--line);font-size:10px;color:var(--muted)}}.badge.ok{{color:#a9f2d9;border-color:rgba(108,229,195,.22);background:rgba(108,229,195,.06)}}.badge.warn{{color:#ffe1a5;border-color:rgba(255,201,107,.22);background:rgba(255,201,107,.06)}}.soft-divider{{height:1px;background:var(--line);margin:15px 0}}.card,.metric-tile,.item>*,.connection-row>*,.connection-summary>*{{min-width:0}}strong,small,.muted,.label,.badge,.connection-main{{overflow-wrap:anywhere;word-break:normal}}pre{{max-width:100%;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}button,input,select,textarea{{min-height:44px}}.connection-summary{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0}}.connection-summary>div{{padding:13px;border:1px solid var(--line);border-radius:13px;background:rgba(255,255,255,.02)}}.connection-summary strong{{display:block;margin-top:6px;font-size:13px}}.connection-list{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}}.connection-row{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:12px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.018)}}.connection-main{{flex:1}}.mission-control{{grid-column:1/-1;padding:20px;background:radial-gradient(circle at 82% 0,rgba(85,184,255,.17),transparent 26rem),linear-gradient(145deg,rgba(15,33,49,.98),rgba(8,21,32,.98));border-color:rgba(85,184,255,.22)}}.mission-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}}.mission-head h2{{display:block;font-size:22px;letter-spacing:-.025em;margin:4px 0 0;color:#f5f9fc}}.mission-state{{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line2);border-radius:999px;padding:7px 10px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;background:rgba(108,229,195,.07);color:#a9f2d9}}.mission-state:before{{content:'';width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 13px currentColor}}.mission-state[data-state='recovering'],.mission-state[data-state='needs_you'],.mission-state[data-state='paused']{{color:#ffe1a5;background:rgba(255,201,107,.07)}}.mission-state[data-state='degraded'],.mission-state[data-state='stopped']{{color:#ffd4d8;background:rgba(255,113,122,.08)}}.mission-grid{{display:grid;grid-template-columns:1.35fr repeat(5,minmax(0,1fr));gap:9px}}.mission-tile{{min-width:0;padding:13px;border:1px solid rgba(164,196,224,.13);border-radius:14px;background:rgba(255,255,255,.026)}}.mission-tile.primary-tile{{background:linear-gradient(145deg,rgba(85,184,255,.1),rgba(108,229,195,.035));border-color:rgba(85,184,255,.21)}}.mission-value{{font-size:17px;font-weight:780;line-height:1.18;letter-spacing:-.025em;overflow-wrap:anywhere}}.mission-value.big{{font-size:22px}}.mission-caption{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.09em;margin-bottom:7px}}.mission-detail{{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.35}}.mission-foot{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}}.mission-foot span{{font-size:11px;color:var(--muted)}}.mission-foot a{{margin-left:auto;font-size:11px;color:#b9dcf7;text-decoration:none}}.system-pill[data-state='recovering'],.system-pill[data-state='needs_you'],.system-pill[data-state='paused']{{border-color:rgba(255,201,107,.25)}}.system-pill[data-state='degraded'],.system-pill[data-state='stopped']{{border-color:rgba(255,113,122,.3)}}.system-pill[data-state='recovering'] .live-dot,.system-pill[data-state='needs_you'] .live-dot,.system-pill[data-state='paused'] .live-dot{{background:var(--warn)}}.system-pill[data-state='degraded'] .live-dot,.system-pill[data-state='stopped'] .live-dot{{background:var(--danger)}}@media(max-width:1200px){{.mission-grid{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}@media(max-width:700px){{.mission-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.mission-head{{align-items:center}}.mission-foot a{{margin-left:0;width:100%}}}}@media(max-width:420px){{.mission-grid{{grid-template-columns:1fr}}.mission-control{{padding:15px}}.mission-head h2{{font-size:19px}}}}@keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(108,229,195,.38)}}70%{{box-shadow:0 0 0 8px rgba(108,229,195,0)}}100%{{box-shadow:0 0 0 0 rgba(108,229,195,0)}}}}@media(max-width:1200px){{.card,.span-6,.span-8{{grid-column:span 6}}.metric-row{{grid-template-columns:repeat(2,minmax(0,1fr))}}.connection-list{{grid-template-columns:1fr}}}}@media(max-width:900px){{nav{{position:sticky;top:0;left:auto;bottom:auto;width:auto;max-width:calc(100vw - 20px);height:auto;margin:10px;display:flex;gap:6px;overflow-x:auto;overflow-y:hidden;padding:9px;border-radius:17px;scrollbar-width:thin}}.brand,.nav-section,.nav-footer{{display:none}}nav a{{flex:0 0 auto;white-space:nowrap;margin:0;padding:9px 11px;min-height:42px}}main{{margin:0;max-width:none;width:100%;padding:10px max(12px,env(safe-area-inset-right)) max(34px,env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}}.topbar{{padding:0 2px;margin-bottom:8px}}.hero{{padding-top:15px}}.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.card,.span-6,.span-8,.wide{{grid-column:1/-1}}.connection-summary{{grid-template-columns:1fr}}}}@media(max-width:640px){{.grid{{grid-template-columns:1fr;gap:10px}}.card{{padding:15px;border-radius:15px}}.metric-row{{grid-template-columns:repeat(2,minmax(0,1fr))}}.form-grid{{grid-template-columns:1fr}}.command-hints{{flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px;scroll-snap-type:x proximity}}.hint{{flex:0 0 auto;scroll-snap-align:start}}.actions,.item,.connection-row{{flex-wrap:wrap}}.item>.actions,.connection-row>.badge{{margin-left:auto}}.hero h1{{font-size:clamp(30px,10vw,44px)}}}}@media(max-width:420px){{.metric-row{{grid-template-columns:1fr}}.command-shell{{display:grid;grid-template-columns:minmax(0,1fr) 48px;padding-left:12px}}.topbar{{flex-wrap:wrap}}.system-pill{{max-width:100%;white-space:normal}}.card h2{{align-items:flex-start;flex-wrap:wrap}}.stat{{font-size:24px}}}}@media(pointer:coarse){{button,nav a,.hint,summary{{min-height:44px}}.card:hover,nav a:hover,.hint:hover{{transform:none}}}}@media(prefers-reduced-motion:reduce){{*,*:before,*:after{{animation:none!important;transition:none!important;scroll-behavior:auto!important}}}}
</style></head><body><nav aria-label='Primary'>
  <div class='brand'><span class='brand-mark' aria-hidden='true'>◇</span><span>LIFE OS</span></div>
  <div class='nav-section'>Control</div>
  <a href='#mission'><span class='nav-dot'></span>Mission Control</a>
  <a href='#now'><span class='nav-dot'></span>Now</a>
  <a href='#awareness'><span class='nav-dot'></span>Awareness</a>
  <a href='#connections'><span class='nav-dot'></span>Connections</a>
  <a href='/command-center'><span class='nav-dot'></span>Central Command</a>
  <a href='#monolith'><span class='nav-dot'></span>MONOLITH</a>
  <a href='#attention'><span class='nav-dot'></span>Needs attention</a>
  <div class='nav-section'>Life</div>
  <a href='#today'><span class='nav-dot'></span>Today</a>
  <a href='#health'><span class='nav-dot'></span>Health</a>
  <a href='#money'><span class='nav-dot'></span>Money</a>
  <a href='#goals'><span class='nav-dot'></span>Goals</a>
  <a href='#purchases'><span class='nav-dot'></span>Purchases</a>
  <a href='/context'><span class='nav-dot'></span>Research &amp; context</a>
  <a href='#domains'><span class='nav-dot'></span>All domains</a>
  <div class='nav-footer'>
    <div id='system-pulse' class='status-line' data-state='live'><span class='live-dot'></span><span>Live awareness active</span></div>
    <div class='tiny'>Local-first · private by default</div>
  </div>
</nav>
<main>
  <div class='topbar'>
    <div id='top-system-state' class='system-pill' data-state='{_esc(snapshot['monolith']['mission']['status'])}' aria-live='polite'><span class='live-dot'></span><span data-status-label>MONOLITH {_esc(snapshot['monolith']['mission']['status_label']).lower()}</span></div>
    <div class='clock' id='live-clock'>--:--</div>
  </div>
  {flash}
  <section class='hero' aria-labelledby='command-title'>
    <div class='eyebrow'>MONOLITH + LIFE OS</div>
    <h1 id='command-title'>What do you want MONOLITH to do?</h1>
    <p>Direct the system from one place. MONOLITH handles execution, recovery and routing; LIFE OS keeps your goals, money, health, devices and real-world context synchronized around it.</p>
    <form method='post' action='/action' class='command-shell'>
      {csrf_quick}
      <input type='hidden' name='request_id' value='{secrets.token_hex(16)}'>
      <input id='universal-command' name='text' maxlength='1000' autocomplete='off' aria-label='Tell LIFE OS what you want to do' placeholder='Tell TEAGAN-PRINCIPAL anything…'>
      <button class='primary' aria-label='Run command'>→</button>
    </form>
    <div class='command-hints' aria-label='Command examples'>
      <button type='button' class='hint' data-command='Help me plan my highest-value action today'>Plan my day</button>
      <button type='button' class='hint' data-command='goal: improve my health system'>Improve my health</button>
      <button type='button' class='hint' data-command='Analyze the best next revenue opportunity and explain what evidence is missing'>Research anything</button>
      <button type='button' class='hint' data-command='purchase: 0 compare my purchase queue'>Review purchases</button>
    </div>
  </section>

  <div class='grid'>
    <section id='mission' class='card mission-control'>
      <div class='mission-head'>
        <div><div class='section-kicker'>Mission Control</div><h2>Autonomy at a glance</h2></div>
        <span id='mission-state' class='mission-state' data-state='{_esc(snapshot['monolith']['mission']['status'])}' aria-live='polite'>{_esc(snapshot['monolith']['mission']['status_label'])}</span>
      </div>
      <div class='mission-grid'>
        <div class='mission-tile primary-tile'><div class='mission-caption'>Doing now</div><div class='mission-value big' id='mission-current'>{_esc(snapshot['monolith']['mission']['current_work']).replace('.', ' › ')}</div><div class='mission-detail'><span id='mission-running'>{snapshot['monolith']['mission']['running_jobs']}</span> running · <span id='mission-engineering-open'>{snapshot['monolith']['mission']['engineering_open']}</span> engineering objectives open</div></div>
        <div class='mission-tile'><div class='mission-caption'>Work queue</div><div class='mission-value big' id='mission-queue'>{snapshot['monolith']['mission']['active_queue']}</div><div class='mission-detail'><span id='mission-throughput'>{snapshot['monolith']['mission']['completed_last_hour']}</span> completed in the last hour · <span id='mission-lifetime'>{snapshot['monolith']['mission']['completed_total']}</span> lifetime</div></div>
        <div class='mission-tile'><div class='mission-caption'>Providers ready</div><div class='mission-value' id='mission-providers'>{_esc(snapshot['monolith']['mission']['provider_summary'])}</div><div class='mission-detail'>Cloud routes can fail over to the local floor; unhealthy routes are not shown as ready.</div></div>
        <div class='mission-tile'><div class='mission-caption'>Verified revenue</div><div class='mission-value big' id='mission-revenue'>{_esc(snapshot['monolith']['mission']['verified_revenue_display'])}</div><div class='mission-detail'><span id='mission-revenue-count'>{snapshot['monolith']['mission']['verified_revenue_count']}</span> authoritative payment record(s)</div></div>
        <div class='mission-tile'><div class='mission-caption'>Needs you</div><div class='mission-value big' id='mission-owner'>{snapshot['monolith']['mission']['owner_gate_count']}</div><div class='mission-detail'>Only genuine owner/human gates. Routine failures should self-repair.</div></div>
        <div class='mission-tile'><div class='mission-caption'>Self-healing</div><div class='mission-value' id='mission-healing'>{_esc(snapshot['monolith']['mission']['self_healing_label'])}</div><div class='mission-detail'><span id='mission-failures'>{len(snapshot['monolith']['mission']['failed_invariants'])}</span> invariant failure(s) · <span id='mission-repairs'>{len(snapshot['monolith']['mission']['repairs_submitted'])}</span> repair(s) submitted</div></div>
      </div>
      <div class='mission-foot'><span>Worker heartbeat: <span id='mission-heartbeat'>{'fresh' if snapshot['monolith']['mission']['heartbeat_fresh'] else 'stale or missing'}</span></span><span>Remote control: <span id='mission-remote'>{_esc(snapshot['monolith']['mission']['remote_control_status'])}</span></span><span>Waiting for provider: <span id='mission-waiting-provider'>{snapshot['monolith']['mission']['engineering_waiting_provider']}</span></span><span>RAM pressure: <span id='mission-memory'>{'yes' if snapshot['monolith']['mission']['memory_pressure'] else 'no'}</span></span><a href='/command-center'>Open deep controls &amp; scheduled work →</a></div>
    </section>

    <section id='now' class='card span-8 now'>
      <div class='section-kicker'>Focus now</div>
      <h2><span>Highest-value next action</span><span class='badge ok'>Live priority</span></h2>
      {now_html}
      <details><summary>Why this?</summary><p class='muted'>LIFE OS ranks active work from your goals, priorities, due dates and current state. Technical routing stays behind the interface unless you ask for it.</p></details>
    </section>

    <section id='awareness' class='card'>
      <h2><span>Live Awareness</span><span class='badge ok'>Reality sync</span></h2>
      <div class='metric-row'>
        <div class='metric-tile'><div class='stat' id='live-fresh'>{awareness['facts_fresh']}</div><div class='label'>Fresh facts</div></div>
        <div class='metric-tile'><div class='stat' id='live-stale'>{awareness['facts_stale']}</div><div class='label'>Stale facts</div></div>
        <div class='metric-tile'><div class='stat' id='live-sources'>{awareness['sources_healthy']}/{awareness['sources_enabled']}</div><div class='label'>Healthy active sources</div></div>
        <div class='metric-tile'><div class='stat' id='live-gaps'>{awareness['requirements_gapped']}</div><div class='label'>Coverage gaps</div></div>
      </div>
      <details><summary>Sources and gaps</summary><h3>Sources</h3>{source_rows}<h3>Coverage gaps</h3>{gap_rows}</details>
    </section>

    <section id='connections' class='card wide'>
      <h2><span>Connections & Live Data</span><span class='badge {'ok' if awareness['external_unconfigured']==0 else 'warn'}'>{awareness['external_connected']}/{awareness['external_total']} external live</span></h2>
      <p class='muted'>This is the connection center for accounts, devices, services and data sources. A source is only shown as connected when LIFE OS has an enabled, working live feed; unconfigured sources are never presented as live.</p>
      <div class='metric-row'>
        <div class='metric-tile'><div class='stat' id='external-connected'>{awareness['external_connected']}</div><div class='label'>External connections live</div></div>
        <div class='metric-tile'><div class='stat' id='external-unconfigured'>{awareness['external_unconfigured']}</div><div class='label'>Need setup / authorization</div></div>
        <div class='metric-tile'><div class='stat'>{local_account_count}</div><div class='label'>LIFE OS accounts tracked</div></div>
        <div class='metric-tile'><div class='stat'>{knowledge_count}</div><div class='label'>Known entities</div></div>
      </div>
      <div class='connection-summary'>
        <div><span class='label'>This device</span><strong>{device_summary}</strong><small>{_esc(device_telemetry)}</small><small>Updated from local device telemetry. No password, token or private key is stored in live facts.</small></div>
        <div><span class='label'>Knowledge store</span><strong>{knowledge_count} entities · {observation_count} observations</strong><small>LIFE OS keeps provenance and timestamps so old information can be distinguished from current reality.</small></div>
      </div>
      <details open><summary>Accounts, devices and services</summary><div class='connection-list'>{connections_html}</div></details>
      <details><summary>What “ready to connect” means</summary><p class='muted'>The adapter slot exists, but the real account or device has not granted access yet. LIFE OS will not invent data or claim a connection. Services that require OAuth, device pairing, banking consent or another provider authorization still require that one-time owner authorization before live synchronization can begin.</p></details>
    </section>

    <section id='requests' class='card wide'><h2>Requests &amp; Results</h2><div id='request-results'>{_request_rows(snapshot, token)}</div></section>
    <section id='monolith' class='card span-6'>
      <h2><span>MONOLITH</span><span class='badge ok'>{_esc(worker_state_label)}</span></h2>
      <div class='metric-row'>
        <div class='metric-tile'><div class='stat' id='queue-count'>{queue_total}</div><div class='label'>Queue state</div></div>
        <div class='metric-tile'><div class='stat'>{len(snapshot['monolith']['verified_revenue'])}</div><div class='label'>Verified revenue records</div></div>
        <div class='metric-tile'><div class='stat' id='approval-count'>{len(snapshot['approvals'])}</div><div class='label'>Approvals</div></div>
        <div class='metric-tile'><div class='stat'>{awareness['requirements_gapped']}</div><div class='label'>Missing live inputs</div></div>
      </div>
      <div class='control-row'>{pause_control}{resume_control}{emergency_control}</div>
      <details><summary>Technical status</summary><pre class='muted'>{_esc(json.dumps(worker, sort_keys=True, default=str, indent=2))}</pre></details>
    </section>

    <section id='activity' class='card span-6'>
      <h2><span>Recent Activity</span><span class='badge ok'>Live</span></h2>
      <div class='activity-list' id='live-activity'>{activity_rows}</div>
    </section>

    <section id='health' class='card'>
      <h2><span>Health</span><span class='badge'>Whole-body</span></h2>
      {health_summary}
      <details><summary>Log a check-in</summary><form method='post' action='/action' class='form-grid'>{csrf_checkin}<input name='sleep_hours' type='number' min='0' max='24' step='0.1' placeholder='Sleep hours'><input name='energy' type='number' min='1' max='10' placeholder='Energy 1-10'><input name='mood' type='number' min='1' max='10' placeholder='Mood 1-10'><input name='pain' type='number' min='0' max='10' placeholder='Pain 0-10'><input name='exercise_minutes' type='number' min='0' value='0' placeholder='Exercise min'><button>Save check-in</button></form></details>
    </section>

    <section id='money' class='card'>
      <p><a href='/money'>Customer payments and accepted orders</a></p>
      <h2><span>Money</span><span class='badge {'ok' if snapshot['net_cash_live'] else 'warn'}'>{'Live' if snapshot['net_cash_live'] else 'Last known'}</span></h2>
      <div class='stat'>{_esc(_money(snapshot['net_cash_cents']))}</div><div class='label'>{_esc(cash_label)}</div><small>{cash_context}</small>
      <details><summary>Accounts and transactions</summary>{money_rows}<form method='post' action='/action' class='form-grid'>{csrf_account}<input name='name' placeholder='Account name' required><input name='balance' type='number' step='0.01' value='0'><button>Add account</button></form><form method='post' action='/action' class='form-grid'>{csrf_tx}<select name='account_id' required><option value=''>Account</option>{''.join(f"<option value='{a['id']}'>{_esc(a['name'])}</option>" for a in snapshot['accounts'])}</select><input name='amount' type='number' step='0.01' placeholder='-23.00 or 1060.00' required><input name='category' placeholder='Category' required><button>Add transaction</button></form></details>
    </section>

    <section id='purchases' class='card'>
      <h2><span>Purchases</span><span class='badge'>{len(snapshot['purchases'])} queued</span></h2>
      {procurement_status}
      {purchase_rows}
      <details><summary>Add purchase</summary><form method='post' action='/action' class='form-grid'>{csrf_purchase}<input name='title' placeholder='Item' required><input name='price' type='number' min='0' step='0.01' placeholder='Price' required><input name='priority' type='number' min='0' max='100' value='50'><label><input name='necessity' type='checkbox' value='1'> Necessary</label><button>Add purchase</button></form></details>
    </section>

    <section id='today' class='card span-8'>
      <h2><span>Today</span><span class='badge'>{len(snapshot['today'])} actions</span></h2>
      {_task_rows(snapshot, token)}
      <details><summary>Add task</summary><form method='post' action='/action' class='form-grid'>{csrf_task}<input name='title' placeholder='New task' required><input name='priority' type='number' min='0' max='100' value='50'><input name='minutes' type='number' min='1' value='30'><input name='due' type='date'><button>Add task</button></form></details>
    </section>

    <section id='goals' class='card'>
      <h2><span>Goals</span><span class='badge'>{len(snapshot['goals'])} active</span></h2>
      {goal_rows}
      <details><summary>Add goal</summary><form method='post' action='/action' class='form-grid'>{csrf_goal}<input name='title' placeholder='Goal' required><input name='priority' type='number' min='0' max='100' value='50'><button>Add goal</button></form></details>
    </section>

    <section id='attention' class='card span-6'>
      <h2><span>Needs Attention</span><span class='badge warn' id='attention-count'>{len(snapshot['attention'])}</span></h2>
      {_attention_rows(snapshot, token)}
      <div class='soft-divider'></div>
      <h3>Risks</h3>{risk_rows}
    </section>

    <section id='approvals' class='card span-6'>
      <h2><span>Approvals</span><span class='badge'>{len(snapshot['approvals'])} waiting</span></h2>
      {_approval_rows(snapshot, token)}
      <p class='muted'>Consequential external actions remain human-authorized. Routine safe work should not interrupt you.</p>
    </section>

    {_autonomy_card(snapshot['autonomy'])}

    <section id='domains' class='card span-6'>
      <h2><span>All LIFE OS Domains</span><span class='badge'>{len(snapshot['domains'])}</span></h2>
      <div class='domain-scroll'>{domain_rows}</div>
      <details><summary>Add a record</summary><form method='post' action='/action' class='form-grid'>{csrf_entity}<select name='domain_key' required>{domain_options}</select><input name='entity_type' placeholder='Record type' required><input name='title' placeholder='Title' required><button>Add record</button></form></details>
    </section>

    <section id='inbox' class='card span-6'>
      <h2><span>Unclassified Inbox</span><span class='badge'>{snapshot['unclassified_count']}</span></h2>
      {inbox_html}
      <p class='muted'>Novel signals are retained instead of discarded so LIFE OS can learn where the ontology needs to expand.</p>
    </section>
  </div>
  <p class='muted' style='margin:22px 4px 0'>LIFE OS is live-aware, not sentient. It knows only what its connected and authorized sources can support, marks stale information, and preserves provenance.</p>
</main></body></html>"""


def _to_int(value: str | None, *, default: int | None = None) -> int | None:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _to_float(value: str | None, *, default: float | None = None) -> float | None:
    if value is None or value.strip() == "":
        return default
    return float(value)


def _dollars_to_cents(value: str) -> int:
    return int(round(float(value) * 100))


def _handle_action(connection: sqlite3.Connection, form: dict[str, list[str]]) -> str:
    result = _perform_action(connection, form)
    try:
        record_owner_action(connection, form.get("action", [""])[0])
    except sqlite3.Error:
        # The domain action has already committed. Do not invite a duplicate
        # transaction/task when optional observation storage temporarily fails.
        connection.rollback()
        logging.getLogger(__name__).warning("Owner-effort observation could not be saved")
    return result


def _perform_action(connection: sqlite3.Connection, form: dict[str, list[str]]) -> str:
    action = form.get("action", [""])[0]
    get = lambda key, default="": form.get(key, [default])[0]

    if action == "quick_add":
        rid = request_fabric.submit(connection, get("text"), request_id=get("request_id") or None)
        immediate = request_fabric.complete_quick_command(connection, rid)
        if immediate:
            return immediate
        return f"Request {rid[:8]} queued. Follow its progress in Requests & Results."
    if action == "request_cancel":
        request_fabric.cancel(connection, get("request_id"))
        return "Request cancelled; completed local work retained"
    if action == "request_resume":
        request_fabric.resume(connection, get("request_id"))
        return "Request resumed from its last verified step"

    if action == "task_add":
        title = get("title").strip()
        priority = int(get("priority", "50"))
        minutes = int(get("minutes", "30"))
        due_text = get("due").strip()
        due = date.fromisoformat(due_text) if due_text else None
        add_task(connection, title, priority, minutes, due)
        return f"Task added: {title}"

    if action == "goal_add":
        title = get("title").strip()
        add_goal(connection, title, int(get("priority", "50")))
        return f"Goal added: {title}"

    if action == "purchase_add":
        title = get("title").strip()
        cents = _dollars_to_cents(get("price"))
        add_purchase(
            connection, title, cents, int(get("priority", "50")),
            get("necessity") == "1",
        )
        return f"Purchase added: {title}"

    if action == "account_add":
        from .finance import add_account
        name = get("name").strip()
        add_account(connection, name, _dollars_to_cents(get("balance", "0")))
        return f"Account added: {name}"

    if action == "transaction_add":
        account_id = int(get("account_id"))
        amount_cents = _dollars_to_cents(get("amount"))
        category = get("category").strip()
        transact(connection, account_id, amount_cents, category)
        return f"Transaction recorded: {_money(amount_cents)}"

    if action == "checkin_save":
        save_checkin(
            connection,
            sleep_hours=_to_float(get("sleep_hours")),
            energy=_to_int(get("energy")),
            mood=_to_int(get("mood")),
            pain=_to_int(get("pain")),
            exercise_minutes=_to_int(get("exercise_minutes"), default=0) or 0,
        )
        return "Health check-in saved"

    if action == "task_done":
        task_id = int(get("task_id"))
        if not LifeOS(connection).finish(task_id):
            raise ValueError("Task not found or already complete")
        return "Task completed"

    if action == "entity_add":
        domain_key = get("domain_key").strip()
        entity_type = get("entity_type").strip()
        title = get("title").strip()
        create_entity(
            connection, entity_type=entity_type, domain_key=domain_key,
            title=title, provenance={"source": "life_os_app"},
        )
        return f"Record added to {DOMAIN_REGISTRY[domain_key].title}"

    if action == "classify_unclassified":
        item_id = get("item_id").strip()
        domain_key = get("domain_key").strip()
        classify_unclassified(connection, item_id, domain_key)
        return f"Item classified as {DOMAIN_REGISTRY[domain_key].title}"

    if action == "known_review":
        import_key = get("import_key").strip()
        decision = get("decision").strip()
        if not mark_reviewed(connection, import_key, decision):
            raise ValueError("Imported fact is no longer pending review")
        return "Imported fact review updated"

    if action == "attention_ack":
        attention_id = int(get("attention_id"))
        if not acknowledge_attention(connection, attention_id):
            raise ValueError("Attention item not found or already acknowledged")
        return "Attention item acknowledged"

    if action == "approval_decide":
        approval_id = int(get("approval_id"))
        decision = get("decision")
        if not decide_approval(connection, approval_id, decision):
            raise ValueError("Approval is no longer pending")
        return f"Approval {decision}"

    if action == "worker_pause":
        initialize_queue(connection)
        set_state(connection, "worker.paused", "1")
        return "Autonomous work paused after the current operation"

    if action == "worker_resume":
        initialize_queue(connection)
        set_state(connection, "worker.emergency_stop", "0")
        set_state(connection, "worker.paused", "0")
        return "Autonomous work resumed"

    if action == "worker_emergency_stop":
        initialize_queue(connection)
        set_state(connection, "worker.emergency_stop", "1")
        set_state(connection, "worker.paused", "1")
        return "Emergency stop engaged; durable state is preserved"

    raise ValueError("Unsupported dashboard action")


def create_app_server(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    """Construct the local server; initialize once, before serving any requests."""
    if host != "127.0.0.1":
        raise ValueError("LIFE OS app must bind strictly to 127.0.0.1")
    path = str(db_path)
    with closing(connect(path)) as connection:
        initialize(connection)
        start_observing(connection)
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _allowed_host(self) -> bool:
            value = self.headers.get("Host", "")
            hostname = value.split(":", 1)[0].lower()
            return hostname in {"127.0.0.1", "localhost"}

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._allowed_host():
                self.send_error(403)
                return
            parsed = urlparse(self.path)
            if parsed.path == "/manifest.webmanifest":
                self._send(200, APP_MANIFEST.encode(), "application/manifest+json; charset=utf-8")
                return
            if parsed.path == "/app.js":
                self._send(200, APP_JS.encode(), "text/javascript; charset=utf-8")
                return
            if parsed.path == "/command-center.js":
                self._send(200, COMMAND_CENTER_JS.encode(), "text/javascript; charset=utf-8")
                return
            if parsed.path == "/sw.js":
                self._send(200, SERVICE_WORKER_JS.encode(), "text/javascript; charset=utf-8")
                return
            if parsed.path == "/icon.svg":
                self._send(200, ICON_SVG.encode(), "image/svg+xml; charset=utf-8")
                return
            if parsed.path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
                return

            if parsed.path in {"/command-center", "/api/command-center"}:
                with closing(connect(path)) as connection:
                    data = command_center_snapshot(connection)
                if parsed.path == "/api/command-center":
                    self._send(
                        200,
                        json.dumps(data, sort_keys=True, default=str).encode(),
                        "application/json; charset=utf-8",
                    )
                else:
                    message = parse_qs(parsed.query).get("msg", [""])[0]
                    self._send(
                        200,
                        render_command_center(data, csrf_token, message).encode(),
                        "text/html; charset=utf-8",
                    )
                return

            if parsed.path == "/money":
                with closing(connect(path)) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    data = collection_snapshot(connection)
                self._send(200, render_money(data, csrf_token).encode(), "text/html; charset=utf-8")
                return

            if parsed.path == "/context":
                query = parse_qs(parsed.query).get("q", [""])[0]
                try:
                    with closing(connect(path)) as connection:
                        connection.execute("PRAGMA query_only=ON")
                        notes = search_notes(connection, query)
                    body = render_context(notes, csrf_token, query=query)
                    self._send(200, body.encode(), "text/html; charset=utf-8")
                except ValueError as exc:
                    body = render_context([], csrf_token, error=str(exc))
                    self._send(400, body.encode(), "text/html; charset=utf-8")
                return

            if parsed.path == "/artifact":
                try:
                    params = parse_qs(parsed.query)
                    rid = params.get("request_id", [""])[0]
                    ordinal = int(params.get("step", ["-1"])[0])
                    with closing(connect(path)) as connection:
                        row = connection.execute("SELECT evidence_json FROM execution_steps WHERE request_id=? AND ordinal=? AND operation='artifact.write' AND state='succeeded'", (rid, ordinal)).fetchone()
                    if row is None:
                        raise ValueError("Artifact not found")
                    evidence = json.loads(row[0])
                    artifact = Path(evidence["path"]).resolve()
                    root = Path(path).resolve().parent / "execution" / "artifacts"
                    if not artifact.is_relative_to(root) or artifact.stat().st_size > 64000:
                        raise ValueError("Artifact outside permitted directory")
                    data = artifact.read_bytes()
                    import hashlib
                    if hashlib.sha256(data).hexdigest() != evidence["sha256"]:
                        raise ValueError("Artifact changed since verification")
                    self._send(200, data, "text/plain; charset=utf-8")
                except (ValueError, OSError, KeyError):
                    self.send_error(404)
                return
            if parsed.path == "/requests":
                with closing(connect(path)) as connection:
                    rows = request_fabric.recent(connection)
                self._send(200, _request_rows({"requests": rows}, csrf_token).encode(), "text/html; charset=utf-8")
                return
            if parsed.path not in {"/", "/api/status"}:
                self.send_error(404)
                return
            connection = connect(path)
            try:
                snapshot = app_snapshot(connection, full_requests=parsed.path != "/api/status")
            finally:
                connection.close()

            if parsed.path == "/api/status":
                body = json.dumps(snapshot, sort_keys=True, default=str).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            if parsed.path != "/":
                self.send_error(404)
                return

            params = parse_qs(parsed.query)
            message = params.get("msg", [""])[0]
            body = _render(snapshot, csrf_token, message).encode()
            self._send(200, body, "text/html; charset=utf-8")

        def do_POST(self):
            if not self._allowed_host():
                self.send_error(403)
                return
            request_path = urlparse(self.path).path
            if request_path not in {"/action", "/context", "/money", "/command-center"}:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 1 or length > MAX_FORM_BYTES:
                self.send_error(413)
                return
            try:
                raw = self.rfile.read(length).decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self.send_error(400)
                return
            form = parse_qs(raw, keep_blank_values=True)
            if form.get("csrf", [""])[0] != csrf_token:
                self.send_error(403)
                return

            if request_path == "/command-center":
                text = form.get("text", [""])[0]
                try:
                    with closing(connect(path)) as connection:
                        command_id = queue_command(connection, text, source="command-center")
                    message = f"Command {command_id[:8]} queued for autonomous execution"
                except (ValueError, TypeError, OverflowError) as exc:
                    message = f"Could not queue command: {exc}"
                self.send_response(303)
                self.send_header("Location", "/command-center?msg=" + quote(message))
                self.end_headers()
                return

            if request_path == "/money":
                error = False
                with closing(connect(path)) as connection:
                    try:
                        message = money_action(connection, form)
                    except (ValueError, TypeError, OverflowError) as exc:
                        connection.rollback()
                        error, message = True, f"Could not save: {exc}"
                    data = collection_snapshot(connection)
                self._send(400 if error else 200, render_money(data, csrf_token, message).encode(), "text/html; charset=utf-8")
                return

            if request_path == "/context":
                query = form.get("q", [""])[0]
                notes = []
                packet = ""
                error = ""
                try:
                    with closing(connect(path)) as connection:
                        connection.execute("PRAGMA query_only=ON")
                        notes = search_notes(connection, query)
                        packet = build_context_packet(connection, form.get("note_id", []))
                except ValueError as exc:
                    error = str(exc)
                body = render_context(notes, csrf_token, query=query[:200], packet=packet, error=error)
                self._send(400 if error else 200, body.encode(), "text/html; charset=utf-8")
                return

            connection = connect(path)
            try:
                message = _handle_action(connection, form)
            except (ValueError, TypeError, OverflowError) as exc:
                connection.rollback()
                message = f"Could not save: {exc}"
            finally:
                connection.close()

            self.send_response(303)
            self.send_header("Location", "/?msg=" + quote(message))
            self.end_headers()

    return ThreadingHTTPServer((host, port), Handler)


def serve_app(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8766,
    *,
    open_browser: bool = True,
) -> None:
    server = create_app_server(db_path, host, port)
    try:
        if open_browser:
            webbrowser.open(f"http://{host}:{server.server_port}/")
        server.serve_forever()
    finally:
        server.server_close()
