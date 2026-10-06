"""Bounded public-web research handoff for LIFE OS.

The local worker owns requests, freshness and evidence storage. A replaceable
research connector performs public-web reads and returns structured citations.
No research result grants execution authority.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .boundaries import validate_external_payload
from .events import append_event
from .sync import acknowledge, emit

TARGET = "research-connector"
SOURCE = "life-os.research.v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_findings(
 request_id TEXT PRIMARY KEY, query TEXT NOT NULL, purpose TEXT NOT NULL,
 summary TEXT NOT NULL, sources_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_research_findings_expiry ON research_findings(expires_at);
"""


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def _request_id(query: str, purpose: str) -> str:
    digest = hashlib.sha256((purpose.strip().lower() + "\n" + query.strip().lower()).encode()).hexdigest()
    return "research:" + digest


def request_research(connection: sqlite3.Connection, *, query: str, purpose: str,
                     freshness_seconds: int = 86400, max_sources: int = 8) -> tuple[str, bool]:
    initialize(connection)
    query = query.strip(); purpose = purpose.strip()
    if not 3 <= len(query) <= 1000 or not 1 <= len(purpose) <= 200:
        raise ValueError("bounded research query and purpose required")
    if not 300 <= freshness_seconds <= 31_536_000:
        raise ValueError("research freshness must be between 5 minutes and 1 year")
    if not 1 <= max_sources <= 12:
        raise ValueError("max_sources must be 1..12")
    request_id = _request_id(query, purpose)
    current = connection.execute("SELECT expires_at FROM research_findings WHERE request_id=?", (request_id,)).fetchone()
    if current and datetime.fromisoformat(current["expires_at"]) > _now_dt():
        return request_id, False
    payload = {
        "query": query,
        "purpose": purpose,
        "freshness_seconds": freshness_seconds,
        "max_sources": max_sources,
        "allowed_actions": ["read_public_web", "return_structured_sources"],
        "execution_authorized": False,
        "external_write_authorized": False,
        "spending_authorized": False,
        "result_schema": "life-os.research-result.v1",
    }
    validate_external_payload(payload)
    _, created = emit(connection, target=TARGET, source=SOURCE, kind="research.public_web",
                      event_id=request_id, correlation_id=request_id, payload=payload)
    if created:
        append_event(connection, "research.requested", {"request_id":request_id,"purpose":purpose})
    return request_id, created


def pending_requests(connection: sqlite3.Connection, limit: int = 25) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100: raise ValueError("limit must be 1..100")
    rows = connection.execute("""SELECT event_id,payload_json,created_at FROM sync_outbox
        WHERE target=? AND source=? AND state!='acked' ORDER BY id LIMIT ?""", (TARGET,SOURCE,limit)).fetchall()
    return [{"request_id":r["event_id"],"created_at":r["created_at"],**json.loads(r["payload_json"])} for r in rows]


def apply_result(connection: sqlite3.Connection, bundle: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    initialize(connection)
    required={"schema_version","request_id","observed_at","summary","sources"}
    if not isinstance(bundle,dict) or set(bundle)!=required or bundle["schema_version"]!=1:
        raise ValueError("unexpected research result schema")
    request_id=bundle["request_id"]
    request=connection.execute("SELECT payload_json FROM sync_outbox WHERE event_id=? AND target=? AND source=?",
                               (request_id,TARGET,SOURCE)).fetchone()
    if request is None: raise ValueError("unknown research request")
    request_payload=json.loads(request["payload_json"])
    observed=datetime.fromisoformat(str(bundle["observed_at"]).replace("Z","+00:00"))
    if observed.tzinfo is None: raise ValueError("research result timestamp requires timezone")
    current=now or _now_dt()
    if observed>current+timedelta(seconds=60) or observed<current-timedelta(days=7):
        raise ValueError("research result is stale or future")
    summary=bundle["summary"]
    if not isinstance(summary,str) or not 1<=len(summary.strip())<=8000: raise ValueError("bounded research summary required")
    sources=bundle["sources"]
    if not isinstance(sources,list) or not 1<=len(sources)<=request_payload["max_sources"]:
        raise ValueError("bounded research sources required")
    normalized=[]
    for source in sources:
        if not isinstance(source,dict) or set(source)!={"title","url","publisher","published_at"}:
            raise ValueError("invalid research source")
        title=str(source["title"]).strip(); url=str(source["url"]).strip(); publisher=str(source["publisher"]).strip()
        published=str(source["published_at"] or "").strip()
        if not 1<=len(title)<=500 or not 8<=len(url)<=2000 or not url.startswith(("https://","http://")) or len(publisher)>300 or len(published)>80:
            raise ValueError("invalid research source fields")
        normalized.append({"title":title,"url":url,"publisher":publisher,"published_at":published})
    validate_external_payload({"summary":summary,"sources":normalized})
    expires=(observed.astimezone(timezone.utc)+timedelta(seconds=int(request_payload["freshness_seconds"]))).isoformat()
    connection.execute("""INSERT INTO research_findings(request_id,query,purpose,summary,sources_json,observed_at,expires_at,created_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET summary=excluded.summary,sources_json=excluded.sources_json,
        observed_at=excluded.observed_at,expires_at=excluded.expires_at""",
        (request_id,request_payload["query"],request_payload["purpose"],summary.strip(),json.dumps(normalized,separators=(",",":"),sort_keys=True),
         observed.astimezone(timezone.utc).isoformat(),expires,_now()))
    connection.commit(); acknowledge(connection,request_id)
    append_event(connection,"research.result_ingested",{"request_id":request_id,"source_count":len(normalized),"execution_authorized":False})
    return {"accepted":True,"request_id":request_id,"expires_at":expires,"source_count":len(normalized)}


def finding(connection: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
    initialize(connection)
    row=connection.execute("SELECT * FROM research_findings WHERE request_id=?",(request_id,)).fetchone()
    if row is None: return None
    item=dict(row); item["sources"]=json.loads(item.pop("sources_json")); item["stale"]=datetime.fromisoformat(item["expires_at"])<=_now_dt()
    return item
