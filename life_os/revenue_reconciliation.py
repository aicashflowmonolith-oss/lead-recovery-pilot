"""Import bounded mailbox evidence into the existing reality store.

This adapter consumes evidence collected by an authorized connector bridge. It
cannot read mail, send messages, grant approvals, or record payment evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .reality import ingest_fact, register_source

SOURCE_KEY = "bridge.revenue_recovery"
FACT_KEY = "monolith.revenue_recovery.prospects"
MAX_BUNDLE_BYTES = 65536


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("An explicit UTC observation timestamp is required") from exc
    if parsed.tzinfo is None:
        raise ValueError("Observation timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_bundle(bundle: dict, *, now: datetime | None = None) -> tuple[str, dict]:
    if not isinstance(bundle, dict) or set(bundle) != {"schema_version", "observed_at", "prospects"}:
        raise ValueError("Unexpected revenue reconciliation fields")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("Unsupported reconciliation schema")
    observed = _time(bundle["observed_at"])
    current = now or datetime.now(timezone.utc)
    if observed > current + timedelta(seconds=60) or observed < current - timedelta(hours=6):
        raise ValueError("Collect a fresh mailbox snapshot; this observation is stale or in the future")
    rows = bundle["prospects"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("A reconciliation must cover 1 to 20 explicitly scoped prospects")
    allowed = {"key", "name", "provider", "outbound_ref", "reply_refs", "evidence_url"}
    seen = set()
    prospects = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != allowed:
            raise ValueError("Unexpected prospect fields; do not include raw email bodies or credentials")
        if not isinstance(row["key"], str) or not re.fullmatch(r"[a-z0-9_-]{1,80}", row["key"]) or row["key"] in seen:
            raise ValueError("Prospect keys must be stable and unique")
        if not isinstance(row["name"], str) or not 1 <= len(row["name"].strip()) <= 160:
            raise ValueError("A bounded business name is required")
        if row["provider"] not in {"gmail", "outlook"}:
            raise ValueError("Only the scoped Gmail and Outlook bridge is supported")
        refs = row["reply_refs"]
        if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(ref, str) or not 1 <= len(ref) <= 512 for ref in refs):
            raise ValueError("Reply evidence references are invalid")
        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate reply evidence")
        if not isinstance(row["outbound_ref"], str) or not 1 <= len(row["outbound_ref"]) <= 512:
            raise ValueError("Confirmed provider outbound evidence is required")
        prefix = "https://mail.google.com/" if row["provider"] == "gmail" else "https://outlook.live.com/"
        if not isinstance(row["evidence_url"], str) or not row["evidence_url"].startswith(prefix) or len(row["evidence_url"]) > 2000:
            raise ValueError("Evidence links must use the expected mail provider")
        seen.add(row["key"])
        prospects.append({**row, "reply_refs": sorted(refs), "reply_status": "observed_needs_triage" if refs else "none_observed"})
    prospects.sort(key=lambda item: item["key"])
    value = {
        "scope": "revenue_recovery_existing_prospects_only",
        "prospects": prospects,
        "outbound_confirmed": len(prospects),
        "prospects_with_replies": sum(bool(row["reply_refs"]) for row in prospects),
        "payment_status": "not_assessed_by_mailbox_reconciliation",
        "execution_authorized": False,
        "delivery_mode": "authorized_connector_bridge",
    }
    if len(json.dumps(value).encode()) > MAX_BUNDLE_BYTES:
        raise ValueError("Reconciliation exceeds the size limit")
    return observed.isoformat(), value


def apply_reconciliation(connection: sqlite3.Connection, bundle: dict, *, now: datetime | None = None) -> dict:
    observed, value = normalize_bundle(bundle, now=now)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    source = connection.execute("SELECT enabled FROM reality_sources WHERE source_key=?", (SOURCE_KEY,)).fetchone()
    if source is not None and not source["enabled"]:
        raise ValueError("Revenue source is disabled; revocation must not be bypassed")
    previous = connection.execute(
        "SELECT source_key,observed_at,value_json FROM reality_facts WHERE fact_key=?", (FACT_KEY,),
    ).fetchone()
    if previous is not None:
        if previous["source_key"] != SOURCE_KEY:
            raise ValueError("Existing fact belongs to another source; reconcile ownership first")
        if _time(previous["observed_at"]) > _time(observed):
            raise ValueError("A newer observation already exists")
        if _time(previous["observed_at"]) == _time(observed):
            if json.loads(previous["value_json"]) != value:
                raise ValueError("Conflicting observations have the same timestamp")
            return {"changed": False, "fact_key": FACT_KEY, "outbound_confirmed": value["outbound_confirmed"], "prospects_with_replies": value["prospects_with_replies"]}
    if source is None:
        register_source(
            connection, source_key=SOURCE_KEY, title="Revenue Recovery mailbox evidence", kind="bridge",
            authority=80, poll_interval_seconds=21600, freshness_seconds=25200, enabled=True,
            config={"scope": "existing revenue-recovery prospect threads", "delivery": "external connector bridge; not a native mailbox poller"},
        )
    result = ingest_fact(
        connection, source_key=SOURCE_KEY, fact_key=FACT_KEY, domain_key="business",
        kind="prospect_mailbox_reconciliation", value=value, observed_at=observed,
        freshness_seconds=25200, metadata={"evidence_digest": digest, "scope": "existing prospects; no payment verification"},
    )
    return {"changed": bool(result["accepted"]), "fact_key": FACT_KEY, "outbound_confirmed": value["outbound_confirmed"], "prospects_with_replies": value["prospects_with_replies"]}
