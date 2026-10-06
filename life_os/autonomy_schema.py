"""SQLite schema for LIFE OS autonomy/control-plane primitives."""
AUTONOMY_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS owner_attention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    source TEXT NOT NULL,
    correlation_id TEXT,
    event_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_owner_attention_open
ON owner_attention(acknowledged_at, created_at DESC);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    risk TEXT NOT NULL,
    cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
    state TEXT NOT NULL DEFAULT 'pending'
      CHECK(state IN ('pending','approved','denied','expired')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, created_at);

CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    health TEXT NOT NULL DEFAULT 'unknown',
    permissions_json TEXT NOT NULL DEFAULT '[]',
    auth_required INTEGER NOT NULL DEFAULT 0 CHECK(auth_required IN (0,1)),
    auth_status TEXT NOT NULL DEFAULT 'unknown',
    cost_fixed_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_fixed_cents >= 0),
    privacy_class TEXT NOT NULL DEFAULT 'public',
    actions_json TEXT NOT NULL DEFAULT '[]',
    reversible INTEGER NOT NULL DEFAULT 1 CHECK(reversible IN (0,1)),
    rate_limit_json TEXT NOT NULL DEFAULT '{}',
    failure_mode TEXT NOT NULL DEFAULT '',
    recovery_method TEXT NOT NULL DEFAULT '',
    owner_approval_required INTEGER NOT NULL DEFAULT 0 CHECK(owner_approval_required IN (0,1)),
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    reliability REAL NOT NULL DEFAULT 0.5 CHECK(reliability BETWEEN 0 AND 1),
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capabilities_route
ON capabilities(enabled, health, kind, priority DESC);

CREATE TABLE IF NOT EXISTS sync_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    correlation_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending'
      CHECK(state IN ('pending','processing','processed','failed')),
    created_at TEXT NOT NULL,
    processed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    correlation_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending'
      CHECK(state IN ('pending','sending','acked','failed')),
    created_at TEXT NOT NULL,
    acked_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sync_inbox_state ON sync_inbox(state, created_at);
CREATE INDEX IF NOT EXISTS idx_sync_outbox_state ON sync_outbox(state, created_at);

CREATE TABLE IF NOT EXISTS payment_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(provider, external_event_id)
);
CREATE INDEX IF NOT EXISTS idx_payment_evidence_status
ON payment_evidence(status, authoritative, observed_at DESC);

CREATE TABLE IF NOT EXISTS human_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    service_area TEXT NOT NULL DEFAULT '',
    contact_ref TEXT NOT NULL DEFAULT '',
    availability_note TEXT NOT NULL DEFAULT '',
    quote_cents INTEGER CHECK(quote_cents IS NULL OR quote_cents >= 0),
    reputation_score REAL NOT NULL DEFAULT 0 CHECK(reputation_score BETWEEN 0 AND 1),
    reputation_evidence TEXT NOT NULL DEFAULT '',
    licensing_insurance_note TEXT NOT NULL DEFAULT '',
    privacy_exposure TEXT NOT NULL DEFAULT 'low',
    reversible INTEGER NOT NULL DEFAULT 1 CHECK(reversible IN (0,1)),
    cancellation_terms TEXT NOT NULL DEFAULT '',
    verification_method TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    updated_at TEXT NOT NULL,
    UNIQUE(name, category, service_area)
);
CREATE INDEX IF NOT EXISTS idx_human_providers_lookup
ON human_providers(category, enabled, quote_cents);

CREATE TABLE IF NOT EXISTS desired_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_key TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,
    controller TEXT NOT NULL,
    desired_json TEXT NOT NULL DEFAULT '{}',
    observed_json TEXT NOT NULL DEFAULT '{}',
    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
    observed_generation INTEGER NOT NULL DEFAULT 0 CHECK(observed_generation >= 0),
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN ('pending','reconciling','in_sync','degraded','blocked','failed','quarantined')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
    next_retry_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_desired_resources_reconcile
ON desired_resources(status, next_retry_at, updated_at);
"""

def initialize_autonomy(connection):
    connection.executescript(AUTONOMY_SCHEMA)
    connection.commit()
