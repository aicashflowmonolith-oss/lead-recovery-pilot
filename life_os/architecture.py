"""Conservative architecture-gap sentinel for LIFE OS.

A module or table proves only that a capability exists, not that it is fully
certified. This sentinel therefore reports implemented / partial / missing and
never upgrades maturity based on documentation alone.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable

from .events import append_event
from .queue import set_state


@dataclass(frozen=True)
class SystemProbe:
    key: str
    title: str
    modules: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    critical: bool = False


SYSTEMS = (
    SystemProbe("authority", "Authority / owner approvals", ("attention",), ("approvals",), True),
    SystemProbe("world_model", "World state / digital twin", ("reality", "foundation"), ("reality_facts", "observations"), True),
    SystemProbe("goals_planning", "Goals, planning and dependencies", ("goals", "planner", "dependencies"), ("goals", "tasks"), True),
    SystemProbe("execution", "Durable execution fabric", ("request_fabric", "execution"), ("execution_requests", "worker_jobs"), True),
    SystemProbe("capabilities", "Capability registry and routing", ("capabilities",), ("capabilities",), True),
    SystemProbe("multi_ai", "Multi-AI routing", ("ai_cli", "capabilities"), ("capabilities",)),
    SystemProbe("engineering", "Engineering / self-upgrade pipeline", ("engineering",), (), True),
    SystemProbe("knowledge", "Second brain / knowledge", ("foundation", "learning", "retention"), ("canonical_entities", "observations")),
    SystemProbe("opportunity", "Opportunity / serendipity engine", ("opportunity",), (), False),
    SystemProbe("sentinels", "Sentinels / watchdogs", ("autonomy_maintenance", "performance", "reality"), (), True),
    SystemProbe("money", "Financial control plane", ("money", "finance", "transactions"), ("payment_evidence", "transactions"), True),
    SystemProbe("procurement", "Procurement", ("purchases",), ("purchases",), False),
    SystemProbe("human_execution", "Human-service broker", ("human_exec",), ("human_providers",), False),
    SystemProbe("experiments", "Experiment platform", ("experiments",), (), False),
    SystemProbe("simulation", "What-if / simulation", ("simulation",), (), False),
    SystemProbe("context", "Context and environment", ("context", "reality"), ("reality_sources",), False),
    SystemProbe("observability", "Audit / observability", ("audit", "events", "metrics"), ("events", "metrics"), True),
    SystemProbe("privacy_security", "Security / privacy boundaries", ("privacy", "boundaries"), (), True),
    SystemProbe("sync", "Protocol / synchronization", ("sync",), ("sync_inbox", "sync_outbox"), True),
    SystemProbe("recovery", "Backup / recovery", ("backup", "autonomy_maintenance"), (), True),
    SystemProbe("safe_mode", "Independent safe-mode kernel", ("safe_mode",), (), True),
    SystemProbe("quotas", "Central quota / autonomy budgets", ("quota",), (), True),
    SystemProbe("storage_lifecycle", "Storage lifecycle / compaction", ("storage_lifecycle", "retention"), (), False),
    SystemProbe("decommissioning", "Capability decommissioning", ("decommission",), (), False),
    SystemProbe("concurrency", "Concurrency / leases / idempotency", ("queue", "sync", "transactions"), ("worker_jobs",), True),
    SystemProbe("communication", "Communication / influence engine", ("communication_engine", "influence"), (), False),
    SystemProbe("research", "External research connector", ("research",), (), False),
    SystemProbe("architecture_gap", "Architecture-gap sentinel", ("architecture",), (), True),
)


def _module_exists(name: str) -> bool:
    return Path(__file__).resolve().with_name(name + ".py").is_file()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _status(probe: SystemProbe, existing_tables: set[str]) -> tuple[str, list[str]]:
    missing_modules = [name for name in probe.modules if not _module_exists(name)]
    missing_tables = [name for name in probe.tables if name not in existing_tables]
    missing = [*("module:" + x for x in missing_modules), *("table:" + x for x in missing_tables)]
    total = len(probe.modules) + len(probe.tables)
    present = total - len(missing)
    if total and present == total:
        return "implemented", []
    if present > 0:
        return "partial", missing
    return "missing", missing


def audit_architecture(connection: sqlite3.Connection) -> dict:
    existing = _tables(connection)
    systems = []
    for probe in SYSTEMS:
        status, missing = _status(probe, existing)
        systems.append({
            "key": probe.key,
            "title": probe.title,
            "status": status,
            "critical": probe.critical,
            "missing": missing,
            "note": "presence is not production certification",
        })
    critical_gaps = [x for x in systems if x["critical"] and x["status"] != "implemented"]
    gaps = [x for x in systems if x["status"] != "implemented"]
    result = {
        "systems_total": len(systems),
        "implemented": sum(x["status"] == "implemented" for x in systems),
        "partial": sum(x["status"] == "partial" for x in systems),
        "missing": sum(x["status"] == "missing" for x in systems),
        "critical_gaps": len(critical_gaps),
        "systems": systems,
    }
    set_state(connection, "architecture.last_gap_scan", json.dumps(result, sort_keys=True))
    append_event(connection, "architecture.gap_scan", {
        "implemented": result["implemented"],
        "partial": result["partial"],
        "missing": result["missing"],
        "critical_gaps": result["critical_gaps"],
        "gap_keys": [x["key"] for x in gaps],
    })
    return result
