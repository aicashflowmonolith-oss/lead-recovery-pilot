"""Idempotent import of owner-provided known state into canonical LIFE OS."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .foundation import _json
from .ontology import normalize_domain


KNOWN_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS known_state_imports (
    import_key TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    source_label TEXT NOT NULL,
    source_observed_at TEXT,
    review_required INTEGER NOT NULL DEFAULT 0 CHECK(review_required IN (0,1)),
    review_state TEXT NOT NULL DEFAULT 'pending'
      CHECK(review_state IN ('pending','confirmed','superseded')),
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_known_state_review
ON known_state_imports(review_required, review_state, source_observed_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_known_state(connection: sqlite3.Connection) -> None:
    connection.executescript(KNOWN_STATE_SCHEMA)
    connection.commit()


def _hash_record(record: dict[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def import_record(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    source_label: str = "prior_conversation",
) -> bool:
    initialize_known_state(connection)
    key = str(record["key"]).strip()
    if not key:
        raise ValueError("known-state key required")
    if connection.execute(
        "SELECT 1 FROM known_state_imports WHERE import_key=?", (key,)
    ).fetchone():
        return False

    entity_id = f"known:{key}"
    domain = normalize_domain(record.get("domain"))
    title = str(record.get("title") or key).strip()
    entity_type = str(record.get("entity_type") or "known_state").strip()
    fact_class = str(record.get("fact_class") or "fact")
    if fact_class not in {"fact", "estimate", "preference", "hypothesis", "recommendation", "unknown"}:
        raise ValueError("invalid known-state fact class")
    confidence = float(record.get("confidence", 1.0))
    if not 0 <= confidence <= 1:
        raise ValueError("known-state confidence must be 0..1")
    privacy_class = str(record.get("privacy_class") or "private")
    status = str(record.get("status") or "active")
    priority = int(record.get("priority", 50))
    metadata = dict(record.get("metadata") or {})
    observed_at = record.get("observed_at")
    review_required = bool(record.get("review_required", False))
    imported_at = _now()

    connection.execute(
        """INSERT INTO canonical_entities(
           id,entity_type,domain_key,title,status,priority,fact_class,confidence,
           privacy_class,risk_level,provenance_json,metadata_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,'normal',?,?,?,?)""",
        (
            entity_id, entity_type, domain, title, status, priority, fact_class,
            confidence, privacy_class,
            _json({"source": source_label, "observed_at": observed_at}),
            _json(metadata), imported_at, imported_at,
        ),
    )

    if "value" in record:
        observation_id = f"knownobs:{key}"
        connection.execute(
            """INSERT OR IGNORE INTO observations(
               id,entity_id,domain_key,kind,value_json,unit,fact_class,source,
               confidence,observed_at,recorded_at,uncertainty_json,
               provenance_json,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id, entity_id, domain,
                str(record.get("kind") or entity_type),
                _json(record.get("value")), str(record.get("unit") or ""),
                fact_class, source_label, confidence,
                str(observed_at or imported_at), imported_at,
                _json(record.get("uncertainty")),
                _json({"source": source_label}),
                _json(metadata),
            ),
        )

    connection.execute(
        """INSERT INTO known_state_imports(
           import_key,entity_id,content_hash,source_label,source_observed_at,
           review_required,review_state,imported_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            key, entity_id, _hash_record(record), source_label, observed_at,
            int(review_required), "pending" if review_required else "confirmed",
            imported_at,
        ),
    )
    connection.commit()
    return True


def import_bundle(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    source_label: str = "prior_conversation",
) -> dict[str, int]:
    added = 0
    skipped = 0
    for record in records:
        if import_record(connection, record, source_label=source_label):
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped, "total": len(records)}


def mark_reviewed(connection: sqlite3.Connection, import_key: str, state: str = "confirmed") -> bool:
    if state not in {"confirmed", "superseded"}:
        raise ValueError("review state must be confirmed or superseded")
    cursor = connection.execute(
        """UPDATE known_state_imports SET review_state=?
           WHERE import_key=? AND review_required=1 AND review_state='pending'""",
        (state, import_key),
    )
    connection.commit()
    return cursor.rowcount == 1


def pending_review(connection: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    initialize_known_state(connection)
    rows = connection.execute(
        """SELECT k.import_key,k.entity_id,k.source_label,k.source_observed_at,
                  e.domain_key,e.entity_type,e.title,e.fact_class,e.confidence
           FROM known_state_imports k
           JOIN canonical_entities e ON e.id=k.entity_id
           WHERE k.review_required=1 AND k.review_state='pending'
           ORDER BY COALESCE(k.source_observed_at,''),k.import_key LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


HISTORICAL_ENTITY_TYPES = {
    "historical_status", "historical_obligation", "historical_purchase",
    "historical_plan", "historical_preference", "event", "lab_history",
    "supplement_history", "performance_snapshot", "network_snapshot",
    "balance_snapshot", "daily_status", "resolved_obligation",
    "hardware_history",
}


def auto_resolve_historical_reviews(connection: sqlite3.Connection) -> int:
    """Accept dated historical records without asking the owner to reconfirm them."""
    initialize_known_state(connection)
    placeholders = ",".join("?" for _ in HISTORICAL_ENTITY_TYPES)
    params = tuple(sorted(HISTORICAL_ENTITY_TYPES))
    cursor = connection.execute(
        f"""UPDATE known_state_imports
            SET review_state='confirmed'
            WHERE review_required=1 AND review_state='pending'
              AND source_observed_at IS NOT NULL
              AND entity_id IN (
                SELECT id FROM canonical_entities
                WHERE entity_type IN ({placeholders}) OR status='historical'
              )""",
        params,
    )
    connection.commit()
    return cursor.rowcount
