"""Canonical whole-life identity, graph, observation, and migration helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .foundation_schema import FOUNDATION_SCHEMA, FOUNDATION_SCHEMA_VERSION
from .ontology import DOMAIN_ALIASES, DOMAIN_SPECS, normalize_domain, require_domain


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True)


def initialize_foundation(connection: sqlite3.Connection) -> None:
    connection.executescript(FOUNDATION_SCHEMA)
    now = _now()
    connection.executemany(
        """INSERT INTO life_domains(key,title,description,active,ontology_version,updated_at)
           VALUES(?,?,?,1,1,?)
           ON CONFLICT(key) DO UPDATE SET title=excluded.title,
             description=excluded.description,active=1,updated_at=excluded.updated_at""",
        [(spec.key, spec.title, spec.description, now) for spec in DOMAIN_SPECS],
    )
    connection.executemany(
        """INSERT INTO domain_aliases(alias,domain_key) VALUES(?,?)
           ON CONFLICT(alias) DO UPDATE SET domain_key=excluded.domain_key""",
        list(DOMAIN_ALIASES.items()),
    )
    connection.execute(
        """INSERT INTO schema_migrations(component,version,applied_at) VALUES('canonical_foundation',?,?)
           ON CONFLICT(component) DO UPDATE SET version=excluded.version,applied_at=excluded.applied_at""",
        (FOUNDATION_SCHEMA_VERSION, now),
    )
    reconcile_legacy_state(connection)
    connection.commit()


def create_entity(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    domain_key: str,
    title: str,
    status: str = "active",
    priority: int = 50,
    fact_class: str = "fact",
    confidence: float = 1.0,
    privacy_class: str = "private",
    risk_level: str = "normal",
    cost_cents: int | None = None,
    value_cents: int | None = None,
    due_at: str | None = None,
    next_action: str = "",
    recurrence: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    entity_id: str | None = None,
) -> sqlite3.Row:
    if not entity_type.strip() or not title.strip():
        raise ValueError("entity type and title are required")
    if not 0 <= priority <= 100:
        raise ValueError("priority must be 0..100")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be 0..1")
    domain = normalize_domain(domain_key)
    identifier = entity_id or str(uuid4())
    now = _now()
    connection.execute(
        """INSERT INTO canonical_entities(
             id,entity_type,domain_key,title,status,priority,fact_class,confidence,
             privacy_class,risk_level,cost_cents,value_cents,due_at,next_action,
             recurrence_json,provenance_json,metadata_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identifier, entity_type.strip(), domain, title.strip(), status.strip(), priority,
            fact_class, confidence, privacy_class, risk_level, cost_cents, value_cents, due_at,
            next_action, _json(recurrence), _json(provenance), _json(metadata), now, now,
        ),
    )
    connection.commit()
    return connection.execute("SELECT * FROM canonical_entities WHERE id=?", (identifier,)).fetchone()


def link_entities(
    connection: sqlite3.Connection,
    source_entity_id: str,
    relation: str,
    target_entity_id: str,
    *,
    confidence: float = 1.0,
    provenance: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    if source_entity_id == target_entity_id or not relation.strip():
        raise ValueError("invalid entity relationship")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be 0..1")
    now = _now()
    cursor = connection.execute(
        """INSERT INTO entity_relationships(
             source_entity_id,relation,target_entity_id,status,confidence,
             provenance_json,metadata_json,created_at,updated_at)
           VALUES(?,?,?,'active',?,?,?,?,?)
           ON CONFLICT(source_entity_id,relation,target_entity_id) DO UPDATE SET
             status='active',confidence=excluded.confidence,
             provenance_json=excluded.provenance_json,metadata_json=excluded.metadata_json,
             updated_at=excluded.updated_at""",
        (source_entity_id, relation.strip(), target_entity_id, confidence,
         _json(provenance), _json(metadata), now, now),
    )
    connection.commit()
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = connection.execute(
        "SELECT id FROM entity_relationships WHERE source_entity_id=? AND relation=? AND target_entity_id=?",
        (source_entity_id, relation.strip(), target_entity_id),
    ).fetchone()
    return int(row["id"])


def record_observation(
    connection: sqlite3.Connection,
    *,
    domain_key: str,
    kind: str,
    value: Any,
    entity_id: str | None = None,
    unit: str = "",
    fact_class: str = "fact",
    source: str = "user",
    confidence: float = 1.0,
    observed_at: str | None = None,
    uncertainty: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    if not kind.strip() or not 0 <= confidence <= 1:
        raise ValueError("invalid observation")
    identifier = str(uuid4())
    recorded = _now()
    connection.execute(
        """INSERT INTO observations(
             id,entity_id,domain_key,kind,value_json,unit,fact_class,source,confidence,
             observed_at,recorded_at,uncertainty_json,provenance_json,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identifier, entity_id, normalize_domain(domain_key), kind.strip(), _json(value), unit,
            fact_class, source, confidence, observed_at or recorded, recorded, _json(uncertainty),
            _json(provenance), _json(metadata),
        ),
    )
    connection.commit()
    return identifier


def capture_unclassified(
    connection: sqlite3.Connection,
    *,
    title: str,
    raw_kind: str,
    raw: Any,
    source: str = "user",
    reason: str = "",
    provenance: dict[str, Any] | None = None,
) -> str:
    entity = create_entity(
        connection, entity_type="unclassified_signal", domain_key="unclassified",
        title=title, fact_class="unknown", confidence=0.0,
        provenance=provenance, metadata={"raw_kind": raw_kind},
    )
    item_id = str(uuid4())
    connection.execute(
        """INSERT INTO unclassified_items(
             id,entity_id,raw_kind,raw_json,source,reason,state,created_at)
           VALUES(?,?,?,?,?,?,'new',?)""",
        (item_id, entity["id"], raw_kind, _json(raw), source, reason, _now()),
    )
    connection.commit()
    return item_id


def classify_unclassified(connection: sqlite3.Connection, item_id: str, domain_key: str) -> None:
    domain = require_domain(domain_key)
    if domain == "unclassified":
        raise ValueError("classification target must be a named domain")
    row = connection.execute(
        "SELECT entity_id FROM unclassified_items WHERE id=? AND state!='dismissed'", (item_id,)
    ).fetchone()
    if not row:
        raise ValueError("unclassified item not found")
    now = _now()
    connection.execute(
        "UPDATE canonical_entities SET domain_key=?,fact_class='fact',updated_at=? WHERE id=?",
        (domain, now, row["entity_id"]),
    )
    connection.execute(
        """UPDATE unclassified_items SET state='classified',classified_domain_key=?,
           classified_at=? WHERE id=?""",
        (domain, now, item_id),
    )
    connection.commit()


def bind_legacy_entity(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    row_id: str | int,
    entity_type: str,
    domain_key: str,
    title: str,
    status: str = "active",
    priority: int = 50,
    cost_cents: int | None = None,
    due_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    identifier = f"legacy:{table_name}:{row_id}"
    now = _now()
    domain = normalize_domain(domain_key)
    body = {"authoritative_table": table_name, **(metadata or {})}
    connection.execute(
        """INSERT INTO canonical_entities(
             id,entity_type,domain_key,title,status,priority,fact_class,confidence,
             privacy_class,risk_level,cost_cents,due_at,provenance_json,metadata_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,'fact',1.0,'private','normal',?,?,'{}',?,?,?)
           ON CONFLICT(id) DO UPDATE SET entity_type=excluded.entity_type,
             domain_key=excluded.domain_key,title=excluded.title,status=excluded.status,
             priority=excluded.priority,cost_cents=excluded.cost_cents,due_at=excluded.due_at,
             metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
        (identifier, entity_type, domain, title, status, priority, cost_cents, due_at, _json(body), now, now),
    )
    connection.execute(
        """INSERT INTO legacy_bindings(table_name,row_id,entity_id,authoritative,last_synced_at)
           VALUES(?,?,?,1,?)
           ON CONFLICT(table_name,row_id) DO UPDATE SET entity_id=excluded.entity_id,
             authoritative=1,last_synced_at=excluded.last_synced_at""",
        (table_name, str(row_id), identifier, now),
    )
    return identifier


def reconcile_legacy_state(connection: sqlite3.Connection) -> int:
    """Index existing module-owned rows without changing their authority."""
    count = 0
    for row in connection.execute("SELECT id,title,status,priority FROM goals"):
        bind_legacy_entity(connection, table_name="goals", row_id=row["id"], entity_type="goal",
                           domain_key="goals", title=row["title"], status=row["status"], priority=row["priority"])
        count += 1
    for row in connection.execute("SELECT id,title,status,priority,due_date,goal_id FROM tasks"):
        bind_legacy_entity(connection, table_name="tasks", row_id=row["id"], entity_type="task",
                           domain_key="goals", title=row["title"], status=row["status"], priority=row["priority"],
                           due_at=row["due_date"], metadata={"goal_id": row["goal_id"]})
        count += 1
    for row in connection.execute("SELECT id,name,balance_cents FROM accounts"):
        bind_legacy_entity(connection, table_name="accounts", row_id=row["id"], entity_type="account",
                           domain_key="money", title=row["name"], metadata={"balance_cents": row["balance_cents"]})
        count += 1
    for row in connection.execute("SELECT id,title,status,priority,price_cents,necessity FROM purchases"):
        bind_legacy_entity(connection, table_name="purchases", row_id=row["id"], entity_type="purchase",
                           domain_key="procurement", title=row["title"], status=row["status"],
                           priority=row["priority"], cost_cents=row["price_cents"],
                           metadata={"necessity": bool(row["necessity"])})
        count += 1
    for row in connection.execute("SELECT id,title,status,domain,starts_at,ends_at FROM commitments"):
        bind_legacy_entity(connection, table_name="commitments", row_id=row["id"], entity_type="commitment",
                           domain_key=row["domain"], title=row["title"], status=row["status"],
                           due_at=row["starts_at"], metadata={"ends_at": row["ends_at"]})
        count += 1
    for row in connection.execute("SELECT id,name,status,domain,kind FROM resources"):
        bind_legacy_entity(connection, table_name="resources", row_id=row["id"], entity_type="resource",
                           domain_key=row["domain"], title=row["name"], status=row["status"],
                           metadata={"kind": row["kind"]})
        count += 1
    for row in connection.execute("SELECT id,name,status,category,recurring_cost_cents FROM dependencies"):
        bind_legacy_entity(connection, table_name="dependencies", row_id=row["id"], entity_type="dependency",
                           domain_key="dependencies_vendors", title=row["name"], status=row["status"],
                           cost_cents=row["recurring_cost_cents"], metadata={"category": row["category"]})
        count += 1
    return count
