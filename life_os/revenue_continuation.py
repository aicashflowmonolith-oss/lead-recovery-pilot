"""Persistent bounded handoffs to the authorized revenue connector.

The local worker schedules work; provider tools execute in the connector session.
Queue state never establishes consent, capability, delivery, payment or authority.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .events import append_event
from .queue import get_state, set_state
from .sync import emit, acknowledge, ingest, mark_processed
from .transactions import DeferredConnection

SOURCE = "revenue-continuation:v1"
TARGET = "revenue-connector"
LANES = ("productized_service", "paid_software_bounties", "digital_deliverables")
DISCOVERY_POLICIES = {
    "paid_software_bounties": {
        "require_original_issue_open": True,
        "require_credible_funded_reward": True,
        "require_low_competition": True,
        "require_zero_upfront_cost": True,
        "require_local_verifiability": True,
        "reject_stale_marketplace_only_state": True,
    }
}
ENABLED = "revenue.continuation.enabled"
KNOWN_STATES = {"candidate", "running", "active", "waiting_external", "waiting_capability",
                "waiting_human", "blocked", "parked", "won", "lost"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def enabled(c):
    return get_state(c, ENABLED) == "1" and not any(
        get_state(c, key) == "1" for key in ("worker.paused", "worker.emergency_stop"))


def _key(branch):
    return SOURCE + ":" + hashlib.sha256(branch.encode()).hexdigest()


def _request(c, branch, kind, payload, now, hours):
    key = _key(branch)
    # One outstanding handoff per branch, even after a long offline interval.
    if c.execute("SELECT 1 FROM sync_outbox WHERE source=? AND correlation_id=? AND state!='acked'",
                 (SOURCE, key)).fetchone():
        return False
    last = get_state(c, key)
    if last and now < _time(last) + timedelta(hours=hours):
        return False
    event_id = key + ":" + hashlib.sha256((last or "initial").encode()).hexdigest()[:20]
    _, created = emit(c, target=TARGET, source=SOURCE, event_id=event_id,
                      correlation_id=key, kind=kind, payload={**payload,
                      "cash_budget_cents": 0, "authority": "read_research_prepare",
                      "commercial_sender": "teagan.holland@outlook.com",
                      "gmail": "historical_read_only", "branch": branch})
    return created


def reconcile(c, *, now=None):
    if not enabled(c):
        return {"enabled": False, "created": 0}
    now = now or datetime.now(timezone.utc)
    created = 0
    # A blocked contact/voice/identity path never prevents independent discovery.
    for lane in LANES:
        payload = {"lane": lane}
        if lane in DISCOVERY_POLICIES:
            payload["discovery_policy"] = DISCOVERY_POLICIES[lane]
        created += _request(c, "discover:" + lane, "revenue.discover", payload, now, 24)
    # Bound each pass and rotate so a large portfolio cannot starve its tail.
    cursor = int(get_state(c, SOURCE + ":cursor") or "0")
    rows = c.execute("SELECT id,external_ref,lane_key,state,human_gate,startup_cost_cents FROM revenue_candidates "
                     "WHERE id>? ORDER BY id LIMIT 100", (cursor,)).fetchall()
    if not rows:
        rows = c.execute("SELECT id,external_ref,lane_key,state,human_gate,startup_cost_cents FROM revenue_candidates "
                         "ORDER BY id LIMIT 100").fetchall()
    for row in rows:
        if row["state"] in {"won", "lost"}:
            continue
        # Human and contact holds receive only evidence reconciliation, never a send.
        payload = dict(row)
        payload["unknown_state"] = row["state"] not in KNOWN_STATES
        created += _request(c, "candidate:" + row["external_ref"], "revenue.reconcile_branch", payload, now, 6)
    if rows:
        set_state(c, SOURCE + ":cursor", str(rows[-1]["id"]))
    # Provider-verified historical outreach may predate candidate registration.
    # Reconcile it too, without inventing economics or creating duplicate prospects.
    from .commercial_mailbox import is_suppressed
    contacts = c.execute("SELECT counterparty,MAX(observed_at) observed_at FROM commercial_mail_events "
                         "WHERE work_ref='' GROUP BY counterparty ORDER BY counterparty LIMIT 100").fetchall()
    for contact in contacts:
        address = contact["counterparty"]
        created += _request(c, "contact:" + address, "revenue.reconcile_contact",
                            {"counterparty": address, "suppressed": is_suppressed(c, address)}, now, 6)
    return {"enabled": True, "created": int(created), "pending": len(pending(c)),
            "native_provider_access": False, "spending_authorized": False}


def pending(c):
    if not enabled(c):
        return []
    rows = c.execute("SELECT event_id,kind,payload_json FROM sync_outbox WHERE source=? AND target=? "
                     "AND state!='acked' ORDER BY id LIMIT 100", (SOURCE, TARGET)).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        # Refresh gates at dispatch; queued snapshots cannot override newer state.
        if row["kind"] == "revenue.reconcile_branch":
            current = c.execute("SELECT state,human_gate,startup_cost_cents FROM revenue_candidates WHERE external_ref=?",
                                (payload["external_ref"],)).fetchone()
            if current:
                payload.update(dict(current))
        if row["kind"] == "revenue.reconcile_contact":
            from .commercial_mailbox import is_suppressed
            payload["suppressed"] = is_suppressed(c, payload["counterparty"])
        result.append({"request_id": row["event_id"], "kind": row["kind"], **payload})
    return result


def complete(c, bundle, *, now=None):
    """Record connector observations atomically, without granting action authority."""
    fields = {"request_id", "observed_at", "status", "summary", "evidence_refs"}
    if not isinstance(bundle, dict) or set(bundle) != fields:
        raise ValueError("unexpected continuation receipt fields")
    if bundle["status"] not in {"observed", "blocked", "no_change"}:
        raise ValueError("unsupported receipt status")
    for key, limit in (("request_id", 200), ("summary", 4000), ("observed_at", 60)):
        if not isinstance(bundle[key], str) or not 1 <= len(bundle[key]) <= limit:
            raise ValueError("invalid receipt " + key)
    refs = bundle["evidence_refs"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 20 or any(
        not isinstance(ref, str) or not 1 <= len(ref) <= 2000 for ref in refs):
        raise ValueError("bounded evidence references required, including blocked outcomes")
    body = _json(bundle)
    if len(body.encode()) > 16384:
        raise ValueError("receipt exceeds 16 KiB")
    now = now or datetime.now(timezone.utc)
    observed = _time(bundle["observed_at"])
    receipt_id = bundle["request_id"] + ":receipt"
    old = c.execute("SELECT payload_json FROM sync_inbox WHERE event_id=?", (receipt_id,)).fetchone()
    if old:
        if old[0] != body:
            raise ValueError("conflicting continuation replay")
        return {"created": False}
    if not enabled(c):
        raise ValueError("continuation disabled or paused")
    if not now - timedelta(hours=6) <= observed <= now + timedelta(seconds=60):
        raise ValueError("fresh connector observation required")
    if c.in_transaction:
        raise ValueError("receipt requires its own transaction")
    c.execute("BEGIN IMMEDIATE")
    try:
        tx = DeferredConnection(c)
        request = c.execute("SELECT correlation_id FROM sync_outbox WHERE event_id=? AND source=? AND state!='acked'",
                            (bundle["request_id"], SOURCE)).fetchone()
        if not request:
            raise ValueError("unknown or closed continuation request")
        ingest(tx, event_id=receipt_id, schema_version=SOURCE, source=TARGET, target="life-os",
               kind="revenue.continuation_receipt", payload=bundle, correlation_id=bundle["request_id"])
        mark_processed(tx, receipt_id)
        acknowledge(tx, bundle["request_id"])
        set_state(tx, request["correlation_id"], now.isoformat())
        append_event(tx, "revenue.continuation.completed", {"request_id": bundle["request_id"],
                     "status": bundle["status"], "revenue_verified": False})
        c.commit()
        return {"created": True, "revenue_verified": False}
    except BaseException:
        c.rollback()
        raise


def status(c):
    return {"enabled": enabled(c), "pending": pending(c),
            "native_provider_access": False, "cash_budget_cents": 0,
            "receipt_count": c.execute("SELECT COUNT(*) FROM sync_inbox WHERE schema_version=?", (SOURCE,)).fetchone()[0]}
