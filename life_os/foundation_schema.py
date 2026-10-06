"""SQLite schema for the canonical whole-life foundation."""
from __future__ import annotations

FOUNDATION_SCHEMA_VERSION = 1

FOUNDATION_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1),
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS life_domains (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    ontology_version INTEGER NOT NULL DEFAULT 1 CHECK(ontology_version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_aliases (
    alias TEXT PRIMARY KEY,
    domain_key TEXT NOT NULL REFERENCES life_domains(key)
);

CREATE TABLE IF NOT EXISTS canonical_entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    domain_key TEXT NOT NULL REFERENCES life_domains(key),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    fact_class TEXT NOT NULL DEFAULT 'fact'
      CHECK(fact_class IN ('fact','estimate','preference','hypothesis','recommendation','unknown')),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    privacy_class TEXT NOT NULL DEFAULT 'private',
    risk_level TEXT NOT NULL DEFAULT 'normal',
    cost_cents INTEGER CHECK(cost_cents IS NULL OR cost_cents >= 0),
    value_cents INTEGER CHECK(value_cents IS NULL OR value_cents >= 0),
    due_at TEXT,
    next_action TEXT NOT NULL DEFAULT '',
    recurrence_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_canonical_entities_domain
ON canonical_entities(domain_key, status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_canonical_entities_type
ON canonical_entities(entity_type, status);

CREATE TABLE IF NOT EXISTS entity_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entity_id TEXT NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    target_entity_id TEXT NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    provenance_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(source_entity_id <> target_entity_id),
    UNIQUE(source_entity_id, relation, target_entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_source
ON entity_relationships(source_entity_id, relation);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_target
ON entity_relationships(target_entity_id, relation);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    entity_id TEXT REFERENCES canonical_entities(id) ON DELETE SET NULL,
    domain_key TEXT NOT NULL REFERENCES life_domains(key),
    kind TEXT NOT NULL,
    value_json TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    fact_class TEXT NOT NULL DEFAULT 'fact'
      CHECK(fact_class IN ('fact','estimate','preference','hypothesis','recommendation','unknown')),
    source TEXT NOT NULL DEFAULT 'user',
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_observations_entity
ON observations(entity_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_domain
ON observations(domain_key, kind, observed_at DESC);

CREATE TABLE IF NOT EXISTS legacy_bindings (
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    entity_id TEXT NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    authoritative INTEGER NOT NULL DEFAULT 1 CHECK(authoritative IN (0,1)),
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY(table_name, row_id),
    UNIQUE(entity_id)
);

CREATE TABLE IF NOT EXISTS unclassified_items (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL UNIQUE REFERENCES canonical_entities(id) ON DELETE CASCADE,
    raw_kind TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'user',
    reason TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'new'
      CHECK(state IN ('new','triaged','classified','dismissed')),
    classified_domain_key TEXT REFERENCES life_domains(key),
    created_at TEXT NOT NULL,
    classified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_unclassified_state
ON unclassified_items(state, created_at DESC);
"""
