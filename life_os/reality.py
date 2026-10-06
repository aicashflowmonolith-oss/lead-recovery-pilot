"""Live reality-sync and awareness primitives for LIFE OS."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .events import append_event
from .foundation import record_observation
from .known_state import auto_resolve_historical_reviews
from .ontology import normalize_domain

REALITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS reality_sources (
    source_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('local','bridge','connector','import','manual')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    authority INTEGER NOT NULL DEFAULT 50 CHECK(authority BETWEEN 0 AND 100),
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300 CHECK(poll_interval_seconds >= 0),
    freshness_seconds INTEGER NOT NULL DEFAULT 900 CHECK(freshness_seconds >= 0),
    health TEXT NOT NULL DEFAULT 'unknown'
      CHECK(health IN ('unknown','healthy','degraded','unavailable','unconfigured')),
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reality_facts (
    fact_key TEXT PRIMARY KEY,
    entity_id TEXT REFERENCES canonical_entities(id) ON DELETE SET NULL,
    domain_key TEXT NOT NULL REFERENCES life_domains(key),
    kind TEXT NOT NULL,
    value_json TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL REFERENCES reality_sources(source_key),
    authority INTEGER NOT NULL CHECK(authority BETWEEN 0 AND 100),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    expires_at TEXT,
    stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1)),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reality_facts_domain
ON reality_facts(domain_key, stale, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_reality_facts_source
ON reality_facts(source_key, stale, observed_at DESC);

CREATE TABLE IF NOT EXISTS reality_requirements (
    fact_key TEXT PRIMARY KEY,
    domain_key TEXT NOT NULL REFERENCES life_domains(key),
    title TEXT NOT NULL,
    max_age_seconds INTEGER NOT NULL DEFAULT 3600 CHECK(max_age_seconds >= 0),
    importance INTEGER NOT NULL DEFAULT 50 CHECK(importance BETWEEN 0 AND 100),
    decision_only INTEGER NOT NULL DEFAULT 1 CHECK(decision_only IN (0,1)),
    preferred_sources_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    updated_at TEXT NOT NULL
);
"""

DEFAULT_SOURCES = (
    ("lifeos.local.system", "This computer", "local", 1, 95, 60, 180, "healthy"),
    ("lifeos.internal.worker", "LIFE OS worker", "local", 1, 100, 30, 90, "healthy"),
    ("lifeos.local.git", "Local Git repositories", "local", 1, 95, 60, 180, "healthy"),
    ("lifeos.internal.accounts", "LIFE OS accounts and balances", "local", 1, 75, 60, 180, "healthy"),
    ("lifeos.internal.data", "LIFE OS knowledge and data store", "local", 1, 90, 60, 180, "healthy"),
    ("known_state", "Imported prior context", "import", 1, 20, 0, 0, "healthy"),
    ("bridge.financial", "Financial accounts", "bridge", 0, 100, 900, 3600, "unconfigured"),
    ("bridge.calendar", "Calendar", "bridge", 0, 100, 3600, 3900, "unconfigured"),
    ("bridge.email", "Email", "bridge", 0, 90, 3600, 3900, "unconfigured"),
    ("bridge.github", "GitHub", "bridge", 0, 100, 3600, 3900, "unconfigured"),
    ("bridge.railway", "Railway", "bridge", 0, 100, 3600, 3900, "unconfigured"),
    ("bridge.health", "Health and wearable data", "bridge", 0, 100, 900, 3600, "unconfigured"),
    ("bridge.orders", "Orders and receipts", "bridge", 0, 90, 1800, 7200, "unconfigured"),
    ("bridge.devices", "Phones, computers and device telemetry", "bridge", 0, 95, 300, 900, "unconfigured"),
    ("bridge.files", "Files and documents", "bridge", 0, 90, 900, 3600, "unconfigured"),
    ("bridge.contacts", "Contacts and people", "bridge", 0, 90, 3600, 7200, "unconfigured"),
    ("bridge.notifications", "Notifications", "bridge", 0, 85, 300, 900, "unconfigured"),
    ("bridge.browser", "Browser and web automation", "bridge", 0, 85, 300, 900, "unconfigured"),
    ("bridge.screen_context", "On-screen context", "bridge", 0, 85, 60, 300, "unconfigured"),
    ("bridge.voice_context", "Voice and spoken context", "bridge", 0, 85, 60, 300, "unconfigured"),
    ("bridge.cloud", "Cloud and infrastructure", "bridge", 0, 95, 900, 3600, "unconfigured"),
    ("bridge.databases", "Databases", "bridge", 0, 95, 900, 3600, "unconfigured"),
    ("bridge.business", "Business and CRM systems", "bridge", 0, 90, 900, 3600, "unconfigured"),
    ("bridge.smart_home", "Home and smart devices", "bridge", 0, 80, 900, 3600, "unconfigured"),
    ("bridge.media", "Media services", "bridge", 0, 75, 1800, 7200, "unconfigured"),
    ("bridge.ai_models", "AI models and agents", "bridge", 0, 95, 300, 900, "unconfigured"),
    ("bridge.messaging", "Messaging services", "bridge", 0, 90, 600, 1800, "unconfigured"),
    ("bridge.location", "Location and mobility context", "bridge", 0, 90, 300, 900, "unconfigured"),
    ("bridge.environment", "Weather and environmental sensors", "bridge", 0, 85, 900, 3600, "unconfigured"),
    ("bridge.transport", "Transport, vehicles and transit", "bridge", 0, 90, 900, 3600, "unconfigured"),
    ("bridge.utilities", "Utilities and household services", "bridge", 0, 85, 3600, 14400, "unconfigured"),
    ("bridge.government", "Government and public-service accounts", "bridge", 0, 95, 3600, 14400, "unconfigured"),
    ("bridge.education", "Education and credential systems", "bridge", 0, 90, 3600, 14400, "unconfigured"),
    ("bridge.insurance", "Insurance providers", "bridge", 0, 95, 3600, 14400, "unconfigured"),
    ("bridge.security", "Identity and security services", "bridge", 0, 95, 900, 3600, "unconfigured"),
    ("bridge.employment", "Work and employment services", "bridge", 0, 90, 3600, 14400, "unconfigured"),
)

SOURCE_CATALOG: dict[str, dict[str, Any]] = {
    "bridge.financial": {"purpose": "Banking, balances, transactions, cards and cashflow state.", "domains": ["money", "business"], "setup": "Connect an authorized financial-data provider; never store raw account numbers or credentials."},
    "bridge.calendar": {"purpose": "Appointments, deadlines, travel blocks and recurring commitments.", "domains": ["time", "goals", "health", "work"], "setup": "Authorize a calendar provider."},
    "bridge.email": {"purpose": "Inbox attention, receipts, confirmations, account notices and commitments.", "domains": ["communications", "procurement", "legal_admin", "business"], "setup": "Authorize an email provider with the minimum required scope."},
    "bridge.github": {"purpose": "Repositories, pull requests, issues and CI state.", "domains": ["ai_automation", "business", "learning"], "setup": "Use an authenticated GitHub connector or local GitHub CLI."},
    "bridge.railway": {"purpose": "Deployments, services, runtime health and infrastructure state.", "domains": ["ai_automation", "dependencies_vendors"], "setup": "Authorize Railway or install/authenticate its CLI."},
    "bridge.health": {"purpose": "Wearables, activity, sleep, vitals, health records and labs.", "domains": ["health", "fitness_sports", "food"], "setup": "Authorize supported health sources; clinical data remains sensitive."},
    "bridge.orders": {"purpose": "Orders, deliveries, receipts, warranties and purchase history.", "domains": ["procurement", "assets", "documents"], "setup": "Connect supported merchant/receipt sources or an email receipt adapter."},
    "bridge.devices": {"purpose": "Phones, computers, wearables and device telemetry.", "domains": ["digital_life", "security_privacy_identity", "assets"], "setup": "Pair or authorize each device/telemetry adapter."},
    "bridge.files": {"purpose": "Files, documents, records and knowledge sources.", "domains": ["data_archive", "documents", "learning"], "setup": "Authorize file/document providers or mount local folders."},
    "bridge.contacts": {"purpose": "People, organizations and contact context.", "domains": ["relationships", "communications", "business"], "setup": "Authorize contacts/directory access."},
    "bridge.notifications": {"purpose": "Device/app notifications and actionable alerts.", "domains": ["communications", "time", "digital_life"], "setup": "Pair a notification-capable device or service."},
    "bridge.browser": {"purpose": "Browser automation, web state and supported site workflows.", "domains": ["ai_automation", "procurement", "business"], "setup": "Authorize a browser/computer-use adapter."},
    "bridge.screen_context": {"purpose": "Understand the screen or active visual context you explicitly choose to share, reducing repeated descriptions and copy/paste.", "domains": ["digital_life", "ai_automation", "work", "learning"], "setup": "Pair an explicit-permission screen-context adapter. Prefer structured app/window context; do not continuously record raw screens by default."},
    "bridge.voice_context": {"purpose": "Turn speech you explicitly choose to share into commands, notes, decisions and context without repeated typing.", "domains": ["communications", "ai_automation", "learning", "personal_development"], "setup": "Pair an explicit-permission microphone/voice adapter. Keep capture visibly controllable and do not retain raw audio by default."},
    "bridge.cloud": {"purpose": "Cloud services, infrastructure and operational health.", "domains": ["ai_automation", "dependencies_vendors", "data_archive"], "setup": "Authorize cloud providers with least privilege."},
    "bridge.databases": {"purpose": "Application databases, schemas, health and business data.", "domains": ["data_archive", "business", "ai_automation"], "setup": "Authorize database connectors with scoped credentials outside LIFE OS facts."},
    "bridge.business": {"purpose": "CRM, sales, leads, support, revenue operations and fulfillment.", "domains": ["business", "work", "communications"], "setup": "Authorize relevant business/CRM services."},
    "bridge.smart_home": {"purpose": "Home sensors, appliances and environmental state.", "domains": ["home", "safety_resilience", "social_environment"], "setup": "Pair supported home hubs/devices."},
    "bridge.media": {"purpose": "Music, video, reading and media usage/subscriptions.", "domains": ["media", "recreation"], "setup": "Authorize selected media services."},
    "bridge.ai_models": {"purpose": "AI providers, agents, availability, cost and execution health.", "domains": ["ai_automation", "dependencies_vendors"], "setup": "Register model/provider adapters without storing provider secrets as facts."},
    "bridge.messaging": {"purpose": "Messaging threads, commitments and unresolved conversations.", "domains": ["communications", "relationships", "business"], "setup": "Authorize supported messaging services."},
    "bridge.location": {"purpose": "Current mobility/location context when explicitly permitted.", "domains": ["location_context", "transportation", "safety_resilience"], "setup": "Pair a device and grant location permission only when desired."},
    "bridge.environment": {"purpose": "Weather, air quality and environmental sensor context.", "domains": ["social_environment", "health", "home"], "setup": "Connect weather/environmental feeds or local sensors."},
    "bridge.transport": {"purpose": "Vehicles, transit, routes, charging/fuel and licence-related logistics.", "domains": ["transportation", "assets"], "setup": "Connect supported transport/vehicle services."},
    "bridge.utilities": {"purpose": "Electricity, internet, water and household service state.", "domains": ["home", "money", "dependencies_vendors"], "setup": "Authorize utility/provider accounts when supported."},
    "bridge.government": {"purpose": "Public-service accounts, benefits, licences and official notices.", "domains": ["legal_admin", "money", "transportation"], "setup": "Use official APIs/exports where permitted; never automate restricted identity steps."},
    "bridge.education": {"purpose": "Courses, credentials, deadlines and education administration.", "domains": ["education", "learning"], "setup": "Authorize education systems when available."},
    "bridge.insurance": {"purpose": "Policies, renewals, claims and coverage state.", "domains": ["insurance", "money", "assets"], "setup": "Authorize insurers or ingest policy documents."},
    "bridge.security": {"purpose": "Account-security posture, breach/device alerts and identity status.", "domains": ["security_privacy_identity", "digital_life"], "setup": "Connect security providers without importing passwords, recovery codes or secrets."},
    "bridge.employment": {"purpose": "Work schedules, applications, contracts and employment administration.", "domains": ["work", "money", "time"], "setup": "Authorize relevant employment/work services."},
}


DEFAULT_REQUIREMENTS = (
    ("system.device_identity", "digital_life", "Current device identity and runtime", 300, 85, 0, ["lifeos.local.system"]),
    ("lifeos.data.coverage", "data_archive", "Current LIFE OS data coverage", 300, 80, 0, ["lifeos.internal.data"]),
    ("money.lifeos_accounts_snapshot", "money", "Current LIFE OS-entered account snapshot", 300, 60, 0, ["lifeos.internal.accounts"]),
    ("money.current_cash", "money", "Current available cash from a financial institution", 3600, 100, 1, ["bridge.financial"]),
    ("calendar.upcoming", "time", "Upcoming commitments", 3900, 95, 1, ["bridge.calendar"]),
    ("email.attention", "communications", "Current inbox attention state", 3900, 80, 1, ["bridge.email"]),
    ("monolith.github_state", "ai_automation", "MONOLITH repository/PR state", 3900, 90, 1, ["bridge.github"]),
    ("monolith.runtime_state", "ai_automation", "MONOLITH deployment state", 3900, 90, 1, ["bridge.railway"]),
    ("health.recent_signals", "health", "Recent health/wearable signals", 3600, 70, 1, ["bridge.health"]),
    ("orders.current", "procurement", "Current orders/receipts", 7200, 65, 1, ["bridge.orders"]),
)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


FORBIDDEN_LIVE_KEYS = frozenset({
    "password", "token", "access_token", "refresh_token", "api_key", "secret",
    "client_secret", "private_key", "recovery_code", "security_answer",
    "account_number", "card_number", "cvv",
})


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True, default=str)


def _reject_secret_data(value: Any) -> None:
    """Live facts may reference accounts/devices, but never carry credentials or raw secrets."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in FORBIDDEN_LIVE_KEYS:
                raise ValueError("live reality facts may not contain credentials or secret identifiers")
            _reject_secret_data(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_data(item)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def initialize_reality(connection: sqlite3.Connection) -> None:
    connection.executescript(REALITY_SCHEMA)
    now = _now()
    for source_key, title, kind, enabled, authority, poll, freshness, health in DEFAULT_SOURCES:
        connection.execute(
            """INSERT INTO reality_sources(
               source_key,title,kind,enabled,authority,poll_interval_seconds,
               freshness_seconds,health,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO NOTHING""",
            (source_key, title, kind, enabled, authority, poll, freshness, health, now),
        )
    for source_key, config in SOURCE_CATALOG.items():
        connection.execute(
            """UPDATE reality_sources SET config_json=?,updated_at=?
               WHERE source_key=? AND (config_json='{}' OR config_json='')""",
            (_json(config), now, source_key),
        )
    for fact_key, domain, title, max_age, importance, decision_only, preferred in DEFAULT_REQUIREMENTS:
        connection.execute(
            """INSERT INTO reality_requirements(
               fact_key,domain_key,title,max_age_seconds,importance,decision_only,
               preferred_sources_json,enabled,updated_at)
               VALUES(?,?,?,?,?,?,?,1,?)
               ON CONFLICT(fact_key) DO NOTHING""",
            (fact_key, normalize_domain(domain), title, max_age, importance, decision_only, _json(preferred), now),
        )
    connection.commit()


def register_source(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    title: str,
    kind: str,
    authority: int = 50,
    poll_interval_seconds: int = 300,
    freshness_seconds: int = 900,
    enabled: bool = True,
    health: str = "unknown",
    config: dict[str, Any] | None = None,
) -> None:
    initialize_reality(connection)
    if kind not in {"local", "bridge", "connector", "import", "manual"}:
        raise ValueError("invalid reality source kind")
    if not 0 <= authority <= 100:
        raise ValueError("authority must be 0..100")
    now = _now()
    connection.execute(
        """INSERT INTO reality_sources(
           source_key,title,kind,enabled,authority,poll_interval_seconds,
           freshness_seconds,health,config_json,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_key) DO UPDATE SET
             title=excluded.title,kind=excluded.kind,enabled=excluded.enabled,
             authority=excluded.authority,poll_interval_seconds=excluded.poll_interval_seconds,
             freshness_seconds=excluded.freshness_seconds,health=excluded.health,
             config_json=excluded.config_json,updated_at=excluded.updated_at""",
        (
            source_key.strip(), title.strip(), kind, int(enabled), authority,
            poll_interval_seconds, freshness_seconds, health, _json(config), now,
        ),
    )
    connection.commit()


def set_source_enabled(connection: sqlite3.Connection, source_key: str, enabled: bool) -> bool:
    cursor = connection.execute(
        "UPDATE reality_sources SET enabled=?,updated_at=? WHERE source_key=?",
        (int(enabled), _now(), source_key),
    )
    connection.commit()
    return cursor.rowcount == 1


def set_source_health(
    connection: sqlite3.Connection,
    source_key: str,
    *,
    health: str,
    error: str = "",
    success: bool = False,
) -> bool:
    if health not in {"unknown", "healthy", "degraded", "unavailable", "unconfigured"}:
        raise ValueError("invalid source health")
    now = _now()
    cursor = connection.execute(
        """UPDATE reality_sources SET health=?,last_checked_at=?,
           last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
           last_error=?,updated_at=? WHERE source_key=?""",
        (health, now, int(success), now, error[:2000], now, source_key),
    )
    connection.commit()
    return cursor.rowcount == 1


def ingest_fact(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    fact_key: str,
    domain_key: str,
    kind: str,
    value: Any,
    entity_id: str | None = None,
    unit: str = "",
    confidence: float = 1.0,
    observed_at: str | None = None,
    freshness_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    initialize_reality(connection)
    source = connection.execute(
        "SELECT * FROM reality_sources WHERE source_key=?", (source_key,)
    ).fetchone()
    if source is None:
        raise ValueError(f"unknown reality source: {source_key}")
    if not source["enabled"]:
        raise ValueError(f"reality source is disabled: {source_key}")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be 0..1")
    observed = observed_at or _now()
    observed_dt = _parse_iso(observed)
    ttl = source["freshness_seconds"] if freshness_seconds is None else freshness_seconds
    expires = None if ttl <= 0 else (observed_dt + timedelta(seconds=ttl)).isoformat()
    domain = normalize_domain(domain_key)
    authority = int(source["authority"])
    ingested = _now()
    _reject_secret_data(value)
    _reject_secret_data(metadata or {})

    record_observation(
        connection,
        entity_id=entity_id,
        domain_key=domain,
        kind=kind,
        value=value,
        unit=unit,
        fact_class="fact",
        source=source_key,
        confidence=confidence,
        observed_at=observed,
        provenance={"source_key": source_key, "authority": authority},
        metadata={"fact_key": fact_key, **(metadata or {})},
    )

    current = connection.execute(
        "SELECT * FROM reality_facts WHERE fact_key=?", (fact_key,)
    ).fetchone()
    accepted = False
    if current is None:
        accepted = True
    else:
        current_observed = _parse_iso(current["observed_at"])
        accepted = (
            authority > current["authority"]
            or (source_key == current["source_key"] and observed_dt >= current_observed)
            or (bool(current["stale"]) and authority >= current["authority"])
        )

    if accepted:
        connection.execute(
            """INSERT INTO reality_facts(
               fact_key,entity_id,domain_key,kind,value_json,unit,source_key,
               authority,confidence,observed_at,ingested_at,expires_at,stale,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)
               ON CONFLICT(fact_key) DO UPDATE SET
                 entity_id=excluded.entity_id,domain_key=excluded.domain_key,kind=excluded.kind,
                 value_json=excluded.value_json,unit=excluded.unit,source_key=excluded.source_key,
                 authority=excluded.authority,confidence=excluded.confidence,
                 observed_at=excluded.observed_at,ingested_at=excluded.ingested_at,
                 expires_at=excluded.expires_at,stale=0,metadata_json=excluded.metadata_json""",
            (
                fact_key, entity_id, domain, kind, _json(value), unit, source_key,
                authority, confidence, observed, ingested, expires, _json(metadata),
            ),
        )
        append_event(
            connection,
            "reality.fact.updated",
            {"fact_key": fact_key, "source_key": source_key, "authority": authority},
        )
    set_source_health(connection, source_key, health="healthy", success=True)
    connection.commit()
    return {"accepted": accepted, "fact_key": fact_key, "source_key": source_key}


def sweep_freshness(connection: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    initialize_reality(connection)
    when = (now or _now_dt()).astimezone(timezone.utc).isoformat()
    newly_stale = connection.execute(
        """UPDATE reality_facts SET stale=1
           WHERE stale=0 AND expires_at IS NOT NULL AND expires_at < ?""",
        (when,),
    ).rowcount
    fresh = connection.execute(
        "SELECT COUNT(*) FROM reality_facts WHERE stale=0"
    ).fetchone()[0]
    stale = connection.execute(
        "SELECT COUNT(*) FROM reality_facts WHERE stale=1"
    ).fetchone()[0]
    connection.commit()
    if newly_stale:
        append_event(connection, "reality.facts.stale", {"count": newly_stale})
    return {"fresh": fresh, "stale": stale, "newly_stale": newly_stale}


def _physical_memory_snapshot() -> dict[str, int] | None:
    """Return physical memory totals using only the standard library."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return {
                    "total_bytes": int(status.ullTotalPhys),
                    "available_bytes": int(status.ullAvailPhys),
                    "used_percent": int(status.dwMemoryLoad),
                }
        elif hasattr(os, "sysconf"):
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
            total = page_size * total_pages
            available = page_size * available_pages
            return {
                "total_bytes": total,
                "available_bytes": available,
                "used_percent": int(round((1 - available / total) * 100)) if total else 0,
            }
    except (AttributeError, OSError, ValueError):
        return None
    return None


def collect_local_system(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_reality(connection)
    source = "lifeos.local.system"
    home = Path.home()
    disk = shutil.disk_usage(home)
    values = {
        "system.device_identity": ("digital_life", "device_identity", {
            "hostname": platform.node(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        }),
        "system.os": ("digital_life", "os_runtime", {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        }),
        "system.python": ("digital_life", "python_runtime", {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        }),
        "system.cpu_logical": ("digital_life", "cpu_logical_count", os.cpu_count()),
        "system.memory_physical": ("digital_life", "physical_memory", _physical_memory_snapshot()),
        "system.home_disk_free": ("digital_life", "disk_free_bytes", disk.free),
        "system.home_disk_total": ("digital_life", "disk_total_bytes", disk.total),
        "lifeos.database.size": ("data_archive", "database_size_bytes",
            (home / ".life-os" / "life.db").stat().st_size if (home / ".life-os" / "life.db").exists() else 0),
    }
    accepted = 0
    for fact_key, (domain, kind, value) in values.items():
        result = ingest_fact(
            connection, source_key=source, fact_key=fact_key,
            domain_key=domain, kind=kind, value=value, confidence=1.0,
        )
        accepted += int(result["accepted"])
    return {"collected": len(values), "accepted": accepted}


def collect_worker_state(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_reality(connection)
    from .queue import get_state, initialize_queue
    initialize_queue(connection)
    heartbeat = get_state(connection, "worker.heartbeat")
    if not heartbeat:
        set_source_health(connection, "lifeos.internal.worker", health="degraded", error="heartbeat missing")
        return {"collected": 0, "reason": "heartbeat missing"}
    try:
        value = json.loads(heartbeat)
    except json.JSONDecodeError:
        value = {"raw": heartbeat}
    result = ingest_fact(
        connection,
        source_key="lifeos.internal.worker",
        fact_key="lifeos.worker.heartbeat",
        domain_key="ai_automation",
        kind="worker_heartbeat",
        value=value,
        confidence=1.0,
        freshness_seconds=90,
    )
    return {"collected": 1, "accepted": int(result["accepted"])}


def collect_internal_accounts(connection: sqlite3.Connection) -> dict[str, Any]:
    """Capture LIFE OS-entered account state without pretending it is bank-live."""
    initialize_reality(connection)
    rows = connection.execute(
        "SELECT id,name,balance_cents FROM accounts ORDER BY id"
    ).fetchall()
    value = {
        "accounts": [
            {"id": int(row["id"]), "name": row["name"], "balance_cents": int(row["balance_cents"])}
            for row in rows
        ],
        "total_cents": sum(int(row["balance_cents"]) for row in rows),
        "source_semantics": "last-known LIFE OS ledger values, not a financial institution feed",
    }
    result = ingest_fact(
        connection,
        source_key="lifeos.internal.accounts",
        fact_key="money.lifeos_accounts_snapshot",
        domain_key="money",
        kind="lifeos_account_snapshot",
        value=value,
        confidence=1.0,
        freshness_seconds=180,
    )
    return {"collected": len(rows), "accepted": int(result["accepted"])}


def collect_internal_data(connection: sqlite3.Connection) -> dict[str, Any]:
    """Expose data-store coverage/size so the app can describe what it actually knows."""
    initialize_reality(connection)
    counts: dict[str, int] = {}
    for table in (
        "canonical_entities", "observations", "goals", "tasks", "purchases",
        "events", "known_state_imports", "reality_facts", "unclassified_items",
    ):
        counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    result = ingest_fact(
        connection,
        source_key="lifeos.internal.data",
        fact_key="lifeos.data.coverage",
        domain_key="data_archive",
        kind="data_coverage_snapshot",
        value=counts,
        confidence=1.0,
        freshness_seconds=180,
    )
    return {"collected": len(counts), "accepted": int(result["accepted"]), "counts": counts}


def _git_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        root = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        if root.returncode != 0:
            return None
        branch = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        remote = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        return {
            "path": str(Path(root.stdout.strip())),
            "branch": branch.stdout.strip(),
            "head": head.stdout.strip(),
            "dirty": bool(status.stdout.strip()),
            "origin": remote.stdout.strip() if remote.returncode == 0 else "",
        }
    except (OSError, subprocess.SubprocessError):
        return None


def collect_local_git(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_reality(connection)
    source = "lifeos.local.git"
    home = Path.home()
    candidates = {
        "life_os": Path(__file__).resolve().parents[1],
        "monolith_mainline": home / "MONOLITH_WORKTREES" / "mainline",
        "ai_agent_army": home / "ai-agent-army",
    }
    collected = 0
    accepted = 0
    for name, path in candidates.items():
        state = _git_state(path)
        if state is None:
            continue
        result = ingest_fact(
            connection,
            source_key=source,
            fact_key=f"git.{name}.state",
            domain_key="ai_automation",
            kind="git_repository_state",
            value=state,
            confidence=1.0,
        )
        collected += 1
        accepted += int(result["accepted"])
    if collected == 0:
        set_source_health(connection, source, health="degraded", error="no local Git repositories detected")
    return {"collected": collected, "accepted": accepted}


def _source_due(connection: sqlite3.Connection, source_key: str, now: datetime | None = None) -> bool:
    initialize_reality(connection)
    row = connection.execute(
        "SELECT enabled,poll_interval_seconds,last_checked_at FROM reality_sources WHERE source_key=?",
        (source_key,),
    ).fetchone()
    if row is None or not row["enabled"]:
        return False
    if not row["last_checked_at"] or int(row["poll_interval_seconds"]) <= 0:
        return True
    elapsed = ((now or _now_dt()) - _parse_iso(row["last_checked_at"])).total_seconds()
    return elapsed >= int(row["poll_interval_seconds"])


def _github_slug_from_origin(origin: str) -> str | None:
    value = origin.strip()
    patterns = (
        r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$",
        r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _run_json_command(args: list[str], timeout: int = 12) -> Any:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "command failed").strip()[:1000])
    return json.loads(result.stdout or "null")


def collect_github_cli(connection: sqlite3.Connection) -> dict[str, Any]:
    """Use an explicitly enabled, already-authenticated GitHub CLI as a read-only source."""
    initialize_reality(connection)
    existing = connection.execute(
        "SELECT enabled,health FROM reality_sources WHERE source_key='bridge.github'"
    ).fetchone()
    if existing is None or not existing["enabled"]:
        return {"collected": 0, "reason": "GitHub source is not enabled"}

    connection.execute(
        """UPDATE reality_sources SET authority=100,poll_interval_seconds=300,
           freshness_seconds=900,config_json=?,updated_at=?
           WHERE source_key='bridge.github'""",
        (_json(SOURCE_CATALOG.get("bridge.github")), _now()),
    )
    connection.commit()
    if not _source_due(connection, "bridge.github"):
        return {"collected": 0, "reason": "not due"}

    gh = shutil.which("gh")
    if not gh:
        set_source_health(connection, "bridge.github", health="unavailable", error="GitHub CLI not installed")
        return {"collected": 0, "reason": "GitHub CLI not installed"}
    try:
        auth = subprocess.run(
            [gh, "auth", "status", "-h", "github.com"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        set_source_health(connection, "bridge.github", health="degraded", error=type(exc).__name__)
        return {"collected": 0, "reason": "GitHub CLI readiness check failed"}
    if auth.returncode != 0:
        set_source_health(connection, "bridge.github", health="unconfigured", error="GitHub CLI is not authenticated")
        return {"collected": 0, "reason": "GitHub CLI is not authenticated"}

    home = Path.home()
    # Only active canonical repositories belong in live GitHub health. Legacy
    # source trees remain available to local inspection but must not degrade
    # canonical repository coverage when their historical remote is unavailable.
    candidates = {
        "life_os": Path(__file__).resolve().parents[1],
        "monolith_mainline": home / "MONOLITH_WORKTREES" / "mainline",
    }
    slugs: dict[str, str] = {}
    for name, path in candidates.items():
        state = _git_state(path)
        if not state:
            continue
        slug = _github_slug_from_origin(state.get("origin", ""))
        if slug:
            slugs[name] = slug

    if not slugs:
        set_source_health(
            connection, "bridge.github", health="degraded",
            error="authenticated GitHub CLI found but no GitHub remotes were detected",
        )
        return {"collected": 0, "reason": "no GitHub remotes"}

    repo_states: dict[str, Any] = {}
    repo_errors: dict[str, str] = {}
    for name, slug in slugs.items():
        try:
            prs = _run_json_command([
                gh, "pr", "list", "--repo", slug, "--state", "open", "--limit", "25",
                "--json", "number,title,state,isDraft,mergeStateStatus,headRefName,baseRefName,updatedAt,url",
            ])
            runs = _run_json_command([
                gh, "run", "list", "--repo", slug, "--limit", "15",
                "--json", "databaseId,status,conclusion,workflowName,headBranch,event,updatedAt,url",
            ])
            repo_states[name] = {"repository": slug, "pull_requests": prs, "workflow_runs": runs}
            ingest_fact(
                connection,
                source_key="bridge.github",
                fact_key=f"github.{name}.state",
                domain_key="ai_automation",
                kind="github_repository_state",
                value=repo_states[name],
                confidence=1.0,
                freshness_seconds=900,
            )
        except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
            repo_errors[name] = str(exc)[:1000]

    if not repo_states:
        error = "; ".join(f"{name}: {message}" for name, message in repo_errors.items()) or "no repositories readable"
        set_source_health(connection, "bridge.github", health="degraded", error=error)
        return {"collected": 0, "reason": error, "repository_errors": repo_errors}

    aggregate = {"repositories": repo_states, "repository_errors": repo_errors}
    ingest_fact(
        connection,
        source_key="bridge.github",
        fact_key="monolith.github_state",
        domain_key="ai_automation",
        kind="github_workspace_state",
        value=aggregate,
        confidence=1.0,
        freshness_seconds=900,
    )
    return {
        "collected": len(repo_states),
        "repositories": sorted(item["repository"] for item in repo_states.values()),
        "repository_errors": repo_errors,
    }


def collect_bridge_inbox(
    connection: sqlite3.Connection,
    bridge_root: Path | None = None,
    *,
    max_files: int = 50,
) -> dict[str, Any]:
    """Consume normalized local connector batches without opening a network listener."""
    initialize_reality(connection)
    root = bridge_root or (Path.home() / ".life-os" / "bridge")
    inbox = root / "inbox"
    processed = root / "processed"
    failed = root / "failed"
    for folder in (inbox, processed, failed):
        folder.mkdir(parents=True, exist_ok=True)

    consumed = 0
    failures = 0
    facts = 0
    for path in sorted(inbox.glob("*.json"))[:max_files]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") != "life-os.reality-batch.v1":
                raise ValueError("unsupported bridge batch schema")
            source_key = str(payload.get("source_key") or "").strip()
            source = connection.execute(
                "SELECT source_key,enabled FROM reality_sources WHERE source_key=?",
                (source_key,),
            ).fetchone()
            if source is None:
                raise ValueError(f"unregistered reality source: {source_key}")
            if not source["enabled"]:
                raise ValueError(f"reality source is not enabled: {source_key}")
            batch = payload.get("facts")
            if not isinstance(batch, list) or not batch:
                raise ValueError("bridge batch must contain a non-empty facts list")
            for item in batch:
                if not isinstance(item, dict):
                    raise ValueError("bridge fact must be an object")
                ingest_fact(
                    connection,
                    source_key=source_key,
                    fact_key=str(item["fact_key"]),
                    domain_key=str(item["domain_key"]),
                    kind=str(item["kind"]),
                    value=item.get("value"),
                    entity_id=item.get("entity_id"),
                    unit=str(item.get("unit") or ""),
                    confidence=float(item.get("confidence", 1.0)),
                    observed_at=item.get("observed_at"),
                    freshness_seconds=item.get("freshness_seconds"),
                    metadata=item.get("metadata"),
                )
                facts += 1
            destination = processed / path.name
            if destination.exists():
                destination.unlink()
            path.replace(destination)
            consumed += 1
        except Exception as exc:
            failures += 1
            destination = failed / path.name
            if destination.exists():
                destination.unlink()
            try:
                path.replace(destination)
                (failed / f"{path.name}.error.txt").write_text(
                    str(exc)[:2000], encoding="utf-8",
                )
            except OSError:
                pass
    return {"files_consumed": consumed, "files_failed": failures, "facts_ingested": facts}


def requirement_gaps(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    initialize_reality(connection)
    rows = connection.execute(
        """SELECT r.*,f.source_key,f.observed_at,f.stale
           FROM reality_requirements r
           LEFT JOIN reality_facts f ON f.fact_key=r.fact_key
           WHERE r.enabled=1
           ORDER BY r.importance DESC,r.fact_key"""
    ).fetchall()
    gaps = []
    now = _now_dt()
    for row in rows:
        missing = row["source_key"] is None
        too_old = False
        if not missing and row["max_age_seconds"] > 0:
            too_old = (now - _parse_iso(row["observed_at"])).total_seconds() > row["max_age_seconds"]
        if missing or bool(row["stale"]) or too_old:
            item = dict(row)
            item["preferred_sources"] = json.loads(item.pop("preferred_sources_json"))
            item["reason"] = "missing" if missing else "stale"
            gaps.append(item)
    return gaps


def source_status(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    initialize_reality(connection)
    out = []
    for row in connection.execute(
        "SELECT * FROM reality_sources ORDER BY enabled DESC,authority DESC,source_key"
    ):
        item = dict(row)
        try:
            item["config"] = json.loads(item.get("config_json") or "{}")
        except json.JSONDecodeError:
            item["config"] = {}
        out.append(item)
    return out


def awareness_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_reality(connection)
    freshness = sweep_freshness(connection)
    sources = source_status(connection)
    gaps = requirement_gaps(connection)
    now = _now_dt()
    for source in sources:
        age_seconds = None
        if source["last_success_at"]:
            age_seconds = max(0.0, (now - _parse_iso(source["last_success_at"])).total_seconds())
        fresh_fact_count = int(connection.execute(
            "SELECT COUNT(*) FROM reality_facts WHERE source_key=? AND stale=0",
            (source["source_key"],),
        ).fetchone()[0])
        live = bool(
            source["enabled"]
            and source["health"] == "healthy"
            and age_seconds is not None
            and (source["freshness_seconds"] <= 0 or age_seconds <= source["freshness_seconds"])
            and (source["kind"] not in {"bridge", "connector"} or fresh_fact_count > 0)
        )
        source["age_seconds"] = age_seconds
        source["fresh_fact_count"] = fresh_fact_count
        source["live"] = live
    configured = [s for s in sources if s["enabled"]]
    healthy = [s for s in configured if s["health"] == "healthy"]
    external = [s for s in sources if s["kind"] in {"bridge", "connector"}]
    external_connected = [s for s in external if s["live"]]
    external_stale = [
        s for s in external if s["enabled"] and not s["live"] and s["health"] != "unconfigured"
    ]
    external_unconfigured = [
        s for s in external if not s["enabled"] or s["health"] == "unconfigured"
    ]
    return {
        "sources_total": len(sources),
        "sources_enabled": len(configured),
        "sources_healthy": len(healthy),
        "external_total": len(external),
        "external_connected": len(external_connected),
        "external_stale": len(external_stale),
        "external_unconfigured": len(external_unconfigured),
        "facts_fresh": freshness["fresh"],
        "facts_stale": freshness["stale"],
        "requirements_gapped": len(gaps),
        "sources": sources,
        "gaps": gaps,
    }


def run_reality_scan(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_reality(connection)
    historical_reviews_resolved = auto_resolve_historical_reviews(connection)
    system = collect_local_system(connection)
    worker = collect_worker_state(connection)
    accounts = collect_internal_accounts(connection)
    data_store = collect_internal_data(connection)
    git = collect_local_git(connection)
    bridge_inbox = collect_bridge_inbox(connection)
    github = collect_github_cli(connection)
    freshness = sweep_freshness(connection)
    snapshot = awareness_snapshot(connection)
    result = {
        "system": system,
        "worker": worker,
        "accounts": accounts,
        "data_store": data_store,
        "git": git,
        "bridge_inbox": bridge_inbox,
        "github": github,
        "freshness": freshness,
        "requirements_gapped": snapshot["requirements_gapped"],
        "historical_reviews_resolved": historical_reviews_resolved,
    }
    append_event(connection, "reality.scan.completed", result)
    return result
