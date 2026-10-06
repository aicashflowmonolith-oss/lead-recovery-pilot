"""Bounded commercial-mail events from authorized provider bridges.

This module never reads mail itself, sends messages, or treats mailbox text as
payment evidence. Provider references remain the audit trail.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from .attention import emit_attention
from .autonomy_brain import record_outcome
from .capabilities import route_capabilities
from .communication_engine import sales_guidance_for_classification
from .events import append_event
from .queue import get_state, set_state
from .revenue_engine import wait_for_capability
from .transactions import DeferredConnection

PROVIDERS={"gmail","outlook"}
DIRECTIONS={"inbound","outbound"}
CLASSIFICATIONS={"reply","buyer_signal","meeting_request","bounce","opt_out","auto_reply","payment_notice","sent"}
CLASSIFICATION_SOURCES={"provider","rule","ai"}
MAX_EVENT_BYTES=16384
SCHEMA="""
CREATE TABLE IF NOT EXISTS commercial_mail_events(
 provider TEXT NOT NULL,message_ref TEXT NOT NULL,thread_ref TEXT NOT NULL,
 direction TEXT NOT NULL,classification TEXT NOT NULL,counterparty TEXT NOT NULL,
 subject TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',work_ref TEXT NOT NULL DEFAULT '',
 required_action TEXT NOT NULL DEFAULT '',classification_source TEXT NOT NULL,confidence REAL NOT NULL,
 evidence_url TEXT NOT NULL,observed_at TEXT NOT NULL,ingested_at TEXT NOT NULL,
 PRIMARY KEY(provider,message_ref));
"""
SCHEMA += """
CREATE INDEX IF NOT EXISTS idx_commercial_mail_events_work
ON commercial_mail_events(work_ref,observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_commercial_mail_events_class
ON commercial_mail_events(classification,observed_at DESC);
"""

def _now()->str:
    return datetime.now(timezone.utc).isoformat()

def _time(value:str)->datetime:
    try: parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except (AttributeError,TypeError,ValueError) as exc: raise ValueError("explicit observation timestamp required") from exc
    if parsed.tzinfo is None: raise ValueError("observation timestamp must include timezone")
    return parsed.astimezone(timezone.utc)

def initialize(c:sqlite3.Connection)->None:
    c.executescript(SCHEMA); c.commit()

def _suppression_key(counterparty:str)->str:
    digest=hashlib.sha256(counterparty.strip().lower().encode()).hexdigest()
    return "commercial.suppressed."+digest

def is_suppressed(c:sqlite3.Connection,counterparty:str)->bool:
    return get_state(c,_suppression_key(counterparty)) is not None

def _bounded_text(value:Any,name:str,limit:int,*,required:bool=False)->str:
    if not isinstance(value,str): raise ValueError(f"{name} must be text")
    value=value.strip()
    if len(value)>limit or (required and not value): raise ValueError(f"invalid {name}")
    return value
def normalize_event(bundle:dict[str,Any],*,now:datetime|None=None)->dict[str,Any]:
    required={"schema_version","provider","message_ref","thread_ref","direction","classification","counterparty",
              "subject","summary","work_ref","required_action","classification_source","confidence","evidence_url","observed_at"}
    if not isinstance(bundle,dict) or set(bundle)!=required: raise ValueError("unexpected commercial mailbox fields")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"]!=1: raise ValueError("unsupported commercial mailbox schema")
    provider=bundle["provider"]
    if provider not in PROVIDERS: raise ValueError("unsupported mailbox provider")
    if bundle["direction"] not in DIRECTIONS: raise ValueError("invalid message direction")
    if bundle["classification"] not in CLASSIFICATIONS: raise ValueError("invalid commercial classification")
    if (bundle["direction"] == "outbound") != (bundle["classification"] == "sent"):
        raise ValueError("outbound events must be sent receipts; commercial signals must be inbound")
    if bundle["classification_source"] not in CLASSIFICATION_SOURCES: raise ValueError("invalid classification source")
    confidence=bundle["confidence"]
    if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0<=confidence<=1: raise ValueError("confidence must be 0..1")
    message_ref=_bounded_text(bundle["message_ref"],"message_ref",512,required=True)
    thread_ref=_bounded_text(bundle["thread_ref"],"thread_ref",512,required=True)
    counterparty=_bounded_text(bundle["counterparty"],"counterparty",254,required=True).lower()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+",counterparty): raise ValueError("provider-verified counterparty email required")
    subject=_bounded_text(bundle["subject"],"subject",300)
    summary=_bounded_text(bundle["summary"],"summary",2000,required=True)
    work_ref=_bounded_text(bundle["work_ref"],"work_ref",512)
    action=_bounded_text(bundle["required_action"],"required_action",80)
    if action and not re.fullmatch(r"[a-z0-9_.-]{1,80}",action): raise ValueError("required_action must be a stable action key")
    if bundle["classification"]=="meeting_request" and not action: raise ValueError("meeting requests must name the machine action required")
    prefix="https://mail.google.com/" if provider=="gmail" else "https://outlook.live.com/"
    evidence=_bounded_text(bundle["evidence_url"],"evidence_url",2000,required=True)
    if not evidence.startswith(prefix): raise ValueError("evidence link must use expected mailbox provider")
    observed=_time(bundle["observed_at"]); current=now or datetime.now(timezone.utc)
    if observed>current+timedelta(seconds=60) or observed<current-timedelta(days=7): raise ValueError("commercial mailbox evidence is stale or future")
    normalized={**bundle,"message_ref":message_ref,"thread_ref":thread_ref,"counterparty":counterparty,
                "subject":subject,"summary":summary,"work_ref":work_ref,"required_action":action,
                "confidence":float(confidence),"evidence_url":evidence,"observed_at":observed.isoformat()}
    if len(json.dumps(normalized,sort_keys=True).encode())>MAX_EVENT_BYTES: raise ValueError("commercial mailbox event exceeds 16 KiB")
    return normalized

def _comparable(row)->dict[str,Any]:
    keys=("provider","message_ref","thread_ref","direction","classification","counterparty","subject","summary",
          "work_ref","required_action","classification_source","confidence","evidence_url","observed_at")
    return {key:row[key] for key in keys}

def ingest_commercial_event(c:sqlite3.Connection,bundle:dict[str,Any],*,now:datetime|None=None)->dict[str,Any]:
    event=normalize_event(bundle,now=now)
    if c.in_transaction:
        raise ValueError("commercial ingestion requires its own transaction")
    # Schema is normally installed by db.initialize; retain standalone compatibility.
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='commercial_mail_events'").fetchone() is None:
        initialize(c)
    c.execute("BEGIN IMMEDIATE")
    try:
        result = _ingest_event(DeferredConnection(c), event)
        c.commit()
        return result
    except BaseException:
        c.rollback()
        raise


def _ingest_event(c, event):
    existing=c.execute("SELECT * FROM commercial_mail_events WHERE provider=? AND message_ref=?",
                       (event["provider"],event["message_ref"])).fetchone()
    if existing is not None:
        expected={key:event[key] for key in _comparable(existing)}
        if _comparable(existing)!=expected: raise ValueError("conflicting commercial mailbox replay")
        return {"created":False,"classification":event["classification"],"work_ref":event["work_ref"]}
    with c:
        c.execute("""INSERT INTO commercial_mail_events(provider,message_ref,thread_ref,direction,classification,counterparty,
          subject,summary,work_ref,required_action,classification_source,confidence,evidence_url,observed_at,ingested_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(event["provider"],event["message_ref"],event["thread_ref"],event["direction"],
          event["classification"],event["counterparty"],event["subject"],event["summary"],event["work_ref"],event["required_action"],
          event["classification_source"],event["confidence"],event["evidence_url"],event["observed_at"],_now()))
    work_ref=event["work_ref"]
    candidate=c.execute("SELECT id,state FROM revenue_candidates WHERE external_ref=?",(work_ref,)).fetchone() if work_ref else None
    outcome=None; waiting_capability=False
    suppressed=is_suppressed(c,event["counterparty"])
    if candidate is not None and event["direction"]=="inbound" and not suppressed:
        outcome={"reply":"reply","buyer_signal":"buyer_signal","meeting_request":"buyer_signal","bounce":"bounce"}.get(event["classification"])
        if outcome:
            record_outcome(c,work_ref,outcome,verified=False,evidence_ref=event["evidence_url"])
        if event["required_action"] and candidate["state"] not in {"waiting_human","blocked","won","lost"}:
            routes=route_capabilities(c,required_actions=[event["required_action"]],allow_owner_approval=False)
            if not routes:
                wait_for_capability(c,int(candidate["id"]),event["required_action"],
                                    blocker=f"{event['classification']} requires machine execution")
                waiting_capability=True
    if event["classification"] in {"opt_out","bounce"}:
        set_state(c,_suppression_key(event["counterparty"]),json.dumps({"provider":event["provider"],
                  "message_ref":event["message_ref"],"observed_at":event["observed_at"]},sort_keys=True,separators=(",",":")))
        suppressed=True
        if candidate is not None and candidate["state"] not in {"waiting_human","won","lost"}:
            c.execute("UPDATE revenue_candidates SET state='blocked',updated_at=? WHERE id=?",(_now(),candidate["id"]))
            c.execute("DELETE FROM revenue_capability_waits WHERE candidate_id=?",(candidate["id"],))
        if candidate is not None:
            c.execute("DELETE FROM revenue_allocations WHERE candidate_id=?",(candidate["id"],))
    attention_id=None
    if not suppressed and event["classification"] in {"buyer_signal","meeting_request"}:
        attention_id,_=emit_attention(c,fingerprint=f"commercial:{event['provider']}:{event['message_ref']}",kind="buyer_signal",
            severity="info",source="commercial-mailbox",event_id=event["message_ref"],payload={"classification":event["classification"],
            "summary":event["summary"],"work_ref":work_ref,"evidence_url":event["evidence_url"],"action_required":False})
    append_event(c,"commercial.mailbox.event_ingested",{"provider":event["provider"],"message_ref":event["message_ref"],
        "classification":event["classification"],"work_ref":work_ref,"outcome":outcome,
        "suppressed":suppressed,"waiting_capability":waiting_capability,"attention_id":attention_id,
        "communication_guidance":sales_guidance_for_classification(event["classification"])})
    return {"created":True,"classification":event["classification"],"work_ref":work_ref,
            "attention_id":attention_id,"waiting_capability":waiting_capability}

def recent_events(c:sqlite3.Connection,limit:int=50)->list[dict[str,Any]]:
    if type(limit) is not int or not 1<=limit<=200: raise ValueError("limit must be 1..200")
    rows=c.execute("""SELECT provider,message_ref,thread_ref,direction,classification,counterparty,subject,summary,
      work_ref,required_action,classification_source,confidence,evidence_url,observed_at,ingested_at
      FROM commercial_mail_events ORDER BY observed_at DESC,provider,message_ref LIMIT ?""",(limit,)).fetchall()
    return [dict(row) for row in rows]
