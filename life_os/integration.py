"""Controlled integration boundary. No personal state leaves LIFE OS implicitly."""
from __future__ import annotations
import sqlite3
import json
from typing import Any
from .attention import emit_attention
from .foundation import record_observation
from .money import record_payment_evidence, validate_payment_evidence
from .sync import ingest

ALLOWED_MONOLITH_FIELDS=("available_work_minutes","business_budget_cents")
ALLOWED_MONOLITH_OWNER_EVENTS={"verified_revenue","buyer_signal","human_gate","milestone"}
MONOLITH_EVENT_DOMAINS={"verified_revenue":"business","buyer_signal":"business","human_gate":"ai_automation","milestone":"goals"}

def monolith_context(c:sqlite3.Connection,available_work_minutes:int,business_budget_cents:int)->dict:
    if available_work_minutes < 0 or business_budget_cents < 0:
        raise ValueError("integration values cannot be negative")
    return {
        "schema":"life-os.monolith-context.v1",
        "available_work_minutes":available_work_minutes,
        "business_budget_cents":business_budget_cents,
    }

def ingest_monolith_owner_event(connection:sqlite3.Connection, *, event_id:str, kind:str,
                                payload:dict[str,Any], correlation_id:str|None=None)->bool:
    if kind not in ALLOWED_MONOLITH_OWNER_EVENTS:
        raise ValueError("MONOLITH owner event kind is not allowed")
    if kind=="verified_revenue":
        required=("provider","external_event_id","amount_cents","currency","status","observed_at","authoritative")
        missing=[k for k in required if k not in payload]
        if missing:
            raise ValueError(f"verified_revenue missing payment evidence fields: {missing}")
        validate_payment_evidence(
            provider=payload["provider"], external_event_id=payload["external_event_id"], evidence_kind="payment",
            amount_cents=payload["amount_cents"], currency=payload["currency"], status=payload["status"],
            observed_at=payload["observed_at"], authoritative=payload["authoritative"], payload={"source":"monolith"},
        )
    _,created=ingest(
        connection,event_id=event_id,schema_version="life-os.monolith-owner.v1",
        source="monolith",target="life-os",kind=kind,payload=payload,correlation_id=correlation_id,
    )
    if not created:
        stored = connection.execute("SELECT kind,payload_json FROM sync_inbox WHERE event_id=?", (event_id,)).fetchone()
        if stored is None or stored["kind"] != kind or json.loads(stored["payload_json"]) != payload:
            raise ValueError("Conflicting MONOLITH event replay")
        if kind != "verified_revenue":
            return False
    safe_payload={k:v for k,v in payload.items() if k not in {"secret","token","credential","password"}}
    if created:
        record_observation(
            connection,domain_key=MONOLITH_EVENT_DOMAINS[kind],kind=f"monolith.{kind}",
            value=safe_payload,source="monolith",confidence=1.0,
            provenance={"event_id":event_id,"correlation_id":correlation_id},
        )
    if kind=="verified_revenue":
        record_payment_evidence(
            connection,
            provider=payload["provider"],
            external_event_id=payload["external_event_id"],
            evidence_kind="payment",
            amount_cents=payload["amount_cents"],
            currency=payload["currency"],
            status=payload["status"],
            observed_at=payload["observed_at"],
            authoritative=payload["authoritative"],
            payload={"source":"monolith"},
        )
    else:
        emit_attention(
            connection,fingerprint=f"monolith:{kind}:{event_id}",kind=kind,
            severity="important" if kind in {"human_gate","buyer_signal"} else "info",
            source="monolith",correlation_id=correlation_id,event_id=event_id,
            payload=safe_payload,
        )
    return created
