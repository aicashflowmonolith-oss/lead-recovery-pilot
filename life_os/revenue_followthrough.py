"""Deterministic, local follow-through for the existing commercial evidence bridge.

The worker owns continuation. The connector supplies read-only observations and
advisory assessments. Neither can authorize or execute a message or payment here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .attention import emit_attention, request_approval
from .autonomy_brain import OUTBOUND_IDENTITY
from .communication_engine import sales_stage_guidance
from .events import append_event
from .queue import get_state, set_state
from .revenue_reconciliation import FACT_KEY, SOURCE_KEY, _time, normalize_bundle
from .sync import acknowledge, emit, ingest, mark_processed

TARGET = "revenue-connector"
PREFIX = "revenue-followthrough:"
ASSESSMENT_KIND = "revenue.reply_assessment"
FIELDS = ("key", "name", "provider", "outbound_ref", "reply_refs", "evidence_url")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _paused(c):
    return any(get_state(c, key) == "1" for key in ("worker.paused", "worker.emergency_stop"))


def _current(c, now):
    source = c.execute("SELECT enabled FROM reality_sources WHERE source_key=?", (SOURCE_KEY,)).fetchone()
    if source is None or not source["enabled"]:
        return "disabled", None, []
    row = c.execute("SELECT * FROM reality_facts WHERE fact_key=?", (FACT_KEY,)).fetchone()
    if row is None:
        return "missing", None, []
    if row["source_key"] != SOURCE_KEY:
        raise ValueError("Revenue fact ownership changed")
    value = json.loads(row["value_json"])
    # Revalidate stored data before using it as machine work, without refreshing it.
    bundle = {"schema_version": 1, "observed_at": row["observed_at"],
              "prospects": [{key: item[key] for key in FIELDS} for item in value["prospects"]]}
    _, validated = normalize_bundle(bundle, now=_time(row["observed_at"]))
    if _time(row["observed_at"]) > now + timedelta(seconds=60):
        raise ValueError("Revenue observation is in the future")
    stale = bool(row["stale"]) or not row["expires_at"] or _time(row["expires_at"]) <= now
    return "stale" if stale else "fresh", row, validated["prospects"]


def _reply_id(prospect):
    return PREFIX + "reply:" + _digest({key: prospect[key] for key in ("key", "provider", "outbound_ref", "reply_refs")})


def _refresh_id(fact):
    return PREFIX + "refresh:" + _digest(None if fact is None else fact["observed_at"])


def _requests(c):
    return c.execute("""SELECT event_id,kind,payload_json,state FROM sync_outbox
        WHERE target=? AND source=? AND state!='acked' ORDER BY id LIMIT 100""", (TARGET, PREFIX)).fetchall()


def _valid_ids(status, fact, prospects):
    if status in {"missing", "stale"}:
        return {_refresh_id(fact)}
    if status == "fresh":
        return {_reply_id(item) for item in prospects if item["reply_refs"]}
    return set()


def pending_requests(c, *, now=None):
    """Read-only machine handoff; stop/revocation/freshness apply at dispatch too."""
    if _paused(c):
        return []
    current = now or datetime.now(timezone.utc)
    status, fact, prospects = _current(c, current)
    valid = _valid_ids(status, fact, prospects)
    return [{"request_id": row["event_id"], "kind": row["kind"], **json.loads(row["payload_json"])}
            for row in _requests(c) if row["event_id"] in valid][:20]


def normalize_assessment(bundle, *, now=None):
    required = {"schema_version", "request_id", "observed_at", "disposition", "summary", "proposal"}
    if not isinstance(bundle, dict) or set(bundle) != required or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("Unexpected assessment schema")
    if not isinstance(bundle["request_id"], str) or not re.fullmatch(PREFIX + r"reply:[a-f0-9]{64}", bundle["request_id"]):
        raise ValueError("An existing reply request is required")
    current = now or datetime.now(timezone.utc)
    observed = _time(bundle["observed_at"])
    if observed > current + timedelta(seconds=60) or observed < current - timedelta(hours=6):
        raise ValueError("Collect a fresh reply assessment")
    if not isinstance(bundle["disposition"], str) or bundle["disposition"] not in {"no_action", "opt_out", "reply_proposed"}:
        raise ValueError("Unsupported disposition")
    if not isinstance(bundle["summary"], str) or not 1 <= len(bundle["summary"].strip()) <= 2000:
        raise ValueError("A bounded, evidence-grounded summary is required")
    proposal = bundle["proposal"]
    if bundle["disposition"] == "reply_proposed":
        if not isinstance(proposal, dict) or set(proposal) != {"sender", "recipient", "subject", "body"}:
            raise ValueError("An exact local reply proposal with sender identity is required")
        if not isinstance(proposal["sender"], str) or proposal["sender"].strip().lower() != OUTBOUND_IDENTITY:
            raise ValueError("Reply proposal sender must match the canonical commercial identity")
        if not isinstance(proposal["recipient"], str) or len(proposal["recipient"]) > 254 or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", proposal["recipient"]):
            raise ValueError("A single provider-verified recipient is required")
        for key, limit in (("subject", 200), ("body", 8000)):
            if not isinstance(proposal[key], str) or not 1 <= len(proposal[key].strip()) <= limit:
                raise ValueError("Reply proposal exceeds its bounds")
        if "\n" in proposal["subject"] or "\r" in proposal["subject"]:
            raise ValueError("Invalid subject")
    elif proposal is not None:
        raise ValueError("Only reply_proposed may contain a proposal")
    if len(_json(bundle).encode()) > 16384:
        raise ValueError("Assessment exceeds 16 KiB")
    return {**bundle, "observed_at": observed.isoformat()}


def apply_assessment(c, bundle, *, now=None):
    """Persist a connector assessment; the worker verifies and advances it later."""
    current = now or datetime.now(timezone.utc)
    normalized = normalize_assessment(bundle, now=current)
    if _paused(c):
        raise ValueError("Autonomous work is paused")
    status, fact, prospects = _current(c, current)
    if status != "fresh" or normalized["request_id"] not in _valid_ids(status, fact, prospects):
        raise ValueError("Reply evidence is unavailable, revoked, stale, or superseded")
    request = c.execute("SELECT event_id FROM sync_outbox WHERE event_id=? AND target=? AND source=?",
                        (normalized["request_id"], TARGET, PREFIX)).fetchone()
    if request is None:
        raise ValueError("Unknown reply request")
    if _time(normalized["observed_at"]) < _time(fact["observed_at"]):
        raise ValueError("Assessment predates its mailbox evidence")
    event_id = normalized["request_id"] + ":assessment"
    previous = c.execute("SELECT payload_json,state,created_at FROM sync_inbox WHERE event_id=?", (event_id,)).fetchone()
    if previous is not None and previous["state"] == "failed":
        try:
            prior_time = _time(json.loads(previous["payload_json"])["observed_at"])
        except (ValueError, TypeError, KeyError):
            prior_time = _time(previous["created_at"])
        if _time(normalized["observed_at"]) <= prior_time:
            raise ValueError("A rejected assessment needs newer evidence")
        c.execute("UPDATE sync_inbox SET payload_json=?,state='pending',error='',processed_at=NULL WHERE event_id=? AND state='failed'",
                  (_json(normalized), event_id))
        c.commit()
        return {"accepted": True, "event_id": event_id}
    if previous is not None:
        if json.loads(previous["payload_json"]) != normalized:
            raise ValueError("Conflicting assessment for this reply evidence")
        return {"accepted": False, "event_id": event_id}
    _, created = ingest(c, event_id=event_id, schema_version="revenue.assessment.v1",
                        source=TARGET, target="life-os", kind=ASSESSMENT_KIND,
                        correlation_id=normalized["request_id"], payload=normalized)
    return {"accepted": created, "event_id": event_id}


def _process_assessment(c, receipt, prospect, fact, now):
    bundle = normalize_assessment(json.loads(receipt["payload_json"]), now=now)
    request_id = _reply_id(prospect)
    if bundle["request_id"] != request_id or receipt["correlation_id"] != request_id:
        raise ValueError("Receipt does not match reply evidence")
    suppressed = get_state(c, "revenue.suppressed." + prospect["key"]) is not None
    disposition = bundle["disposition"]
    if disposition == "opt_out":
        # Never clear suppression automatically when later mail arrives.
        set_state(c, "revenue.suppressed." + prospect["key"], _json({"request_id": request_id, "observed_at": bundle["observed_at"]}))
    approval_id = None
    if disposition == "reply_proposed" and not suppressed:
        proposal = bundle["proposal"]
        payload = {"proposal": proposal, "required_sender": OUTBOUND_IDENTITY,
                   "summary": bundle["summary"], "evidence_url": prospect["evidence_url"],
                   "request_id": request_id, "reply_refs": prospect["reply_refs"], "assessment_class": "ai_inferred",
                   "communication_guidance": sales_stage_guidance("follow_up"),
                   "execution_authorized": False, "scope": "Exact proposed reply only; no pilot, spending or payment authority"}
        approval_id, _ = request_approval(c, fingerprint=request_id, action="Review reply to " + prospect["name"],
                                         risk="external_contact", payload=payload, expires_at=fact["expires_at"])
        emit_attention(c, fingerprint=request_id, kind="human_gate", source=PREFIX,
                       payload={"approval_id": approval_id, "summary": "Exact reply and evidence are ready for review"})
    # Every write above is idempotent so interruption before acknowledgment is repairable.
    acknowledge(c, request_id)
    mark_processed(c, receipt["event_id"])
    append_event(c, "autonomy.revenue.assessment_completed", {"request_id": request_id,
                 "disposition": disposition, "suppressed": suppressed or disposition == "opt_out",
                 "approval_id": approval_id})


def reconcile(c, *, now=None):
    """Select and execute only bounded local actions using canonical evidence."""
    current = now or datetime.now(timezone.utc)
    if _paused(c):
        return {"state": "paused", "requests_created": 0, "assessments_completed": 0}
    status, fact, prospects = _current(c, current)
    valid = _valid_ids(status, fact, prospects)
    created = completed = 0
    if status in {"missing", "stale"}:
        _, added = emit(c, target=TARGET, source=PREFIX, kind="revenue.refresh_evidence",
                        event_id=_refresh_id(fact), payload={"scope": "existing_revenue_prospects_only",
                        "observed_at": None if fact is None else fact["observed_at"],
                        "prospects": [{key: item[key] for key in FIELDS} for item in prospects],
                        "allowed_actions": ["read_existing_threads", "import_confirmed_evidence"], "execution_authorized": False})
        created += int(added)
    elif status == "fresh":
        for prospect in prospects:
            if not prospect["reply_refs"]:
                continue
            request_id = _reply_id(prospect)
            _, added = emit(c, target=TARGET, source=PREFIX, kind="revenue.inspect_reply", event_id=request_id,
                            payload={"prospect": {key: prospect[key] for key in FIELDS}, "observed_at": fact["observed_at"],
                            "allowed_actions": ["read_existing_thread", "prepare_local_assessment"],
                            "communication_guidance": sales_stage_guidance("follow_up"), "execution_authorized": False})
            created += int(added)
            receipt = c.execute("""SELECT * FROM sync_inbox WHERE event_id=? AND state='pending'
                AND source=? AND target='life-os' AND kind=?""", (request_id + ":assessment", TARGET, ASSESSMENT_KIND)).fetchone()
            if receipt is not None:
                try:
                    _process_assessment(c, receipt, prospect, fact, current)
                    completed += 1
                except (ValueError, KeyError, TypeError):
                    # A bad advisory receipt must not starve other prospects or worker jobs.
                    mark_processed(c, receipt["event_id"], "invalid_or_expired_assessment")
                    append_event(c, "autonomy.revenue.assessment_rejected", {"request_id": request_id})
            if get_state(c, "revenue.suppressed." + prospect["key"]) is None:
                # A refreshed observation of the same exact reply keeps its pending
                # proposal reviewable. Never reopen an owner's denied/approved decision.
                renewed = c.execute("""UPDATE approvals SET state='pending',decided_at=NULL,expires_at=?
                    WHERE fingerprint=? AND state IN ('pending','expired')
                    AND (expires_at IS NULL OR expires_at<?)""",
                    (fact["expires_at"], request_id, fact["expires_at"]))
                c.commit()
                if renewed.rowcount:
                    append_event(c, "autonomy.revenue.approval_evidence_refreshed", {"request_id": request_id})
        # Retire machine requests only once current evidence proves they are obsolete.
        for request in _requests(c):
            if request["event_id"] not in valid:
                acknowledge(c, request["event_id"])
                c.execute("UPDATE approvals SET state='expired',decided_at=? WHERE fingerprint=? AND state='pending'",
                          (current.isoformat(), request["event_id"]))
                c.commit()
    if status in {"fresh", "disabled"}:
        for approval in c.execute("SELECT id,fingerprint FROM approvals WHERE state='pending' AND fingerprint LIKE ?", (PREFIX + "%",)).fetchall():
            if approval["fingerprint"] not in valid:
                c.execute("UPDATE approvals SET state='expired',decided_at=? WHERE id=? AND state='pending'", (current.isoformat(), approval["id"]))
        c.commit()
    result = {"state": status, "requests_created": created, "assessments_completed": completed,
              "prospects_observed": len(prospects), "prospects_with_replies": sum(bool(p["reply_refs"]) for p in prospects),
              "observation_at": None if fact is None else fact["observed_at"],
              "external_execution_authorized": False}
    set_state(c, "revenue.last_followthrough", _json(result))
    return result


def approval_is_current(c, fingerprint, *, now=None):
    """Check current evidence at the decision boundary, not just at scan time."""
    if _paused(c):
        return False
    try:
        status, fact, prospects = _current(c, now or datetime.now(timezone.utc))
        return status == "fresh" and any(_reply_id(p) == fingerprint and
            get_state(c, "revenue.suppressed." + p["key"]) is None for p in prospects if p["reply_refs"])
    except (ValueError, KeyError, TypeError):
        return False


def connector_handoff(c, *, now=None):
    """Give the existing monitor its scope and stop state without manual copying."""
    current = now or datetime.now(timezone.utc)
    if _paused(c):
        return {"state": "paused", "prospects": [], "requests": []}
    status, fact, prospects = _current(c, current)
    return {"state": status, "observed_at": None if fact is None else fact["observed_at"],
            "scope": "existing_revenue_prospects_only",
            "prospects": [{key: item[key] for key in FIELDS} for item in prospects],
            "requests": pending_requests(c, now=current)}
