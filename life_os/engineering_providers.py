"""Engineering specialists in the existing registry, never a second queue.

Provider capabilities represent execution hosts. OpenCode model routes have
independent circuits so one bad cloud model cannot disable the whole provider.
Ollama is the zero-cost local provider floor: it is deliberately lowest
priority, requires no cloud authentication, and may only operate through
MONOLITH's bounded local adapter inside an isolated worktree.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .ai_cli import AdapterError, CapabilityUnavailable
from .capabilities import (
    list_capabilities, record_capability_failure, record_capability_success,
    route_capabilities, upsert_capability,
)
from .events import append_event
from .opencode_policy import (
    OPENCODE_INTERNAL_MODELS, OPENCODE_POLICY_CHECKED_ON,
    OPENCODE_POLICY_SOURCE, permits_internal_model,
)

ADAPTERS = ("codex", "opencode", "ollama")
PREFIX = "engineering.cli."
MODEL_ADAPTER = "life_os.engineering.model.v1"
MODEL_PREFIX = "engineering.model."
OPENCODE_MODELS = OPENCODE_INTERNAL_MODELS


def _model_name(model: str) -> str:
    return MODEL_PREFIX + model


def ensure_opencode_model_routes(connection):
    existing = {cap["name"]: cap for cap in list_capabilities(connection)}
    for index, model in enumerate(OPENCODE_MODELS):
        name = _model_name(model)
        if name in existing:
            continue
        upsert_capability(
            connection, name=name, kind="cli", enabled=True, health="healthy",
            permissions=["isolated_workspace_only"], auth_required=False, auth_status="ready",
            cost_fixed_cents=0, privacy_class="internal", actions=["engineering.build.model"],
            owner_approval_required=False, priority=100-index, reliability=0.5, latency_ms=0,
            metadata={
                "adapter": MODEL_ADAPTER, "provider": "opencode", "model": model,
                "qualified_zero_cost": True, "routing_scope": "model",
                "input_policy": "zero_retention_no_training",
                "policy_source": OPENCODE_POLICY_SOURCE,
                "policy_checked_on": OPENCODE_POLICY_CHECKED_ON,
            },
        )
    return [cap for cap in list_capabilities(connection) if cap["name"].startswith(MODEL_PREFIX)]


def eligible_opencode_models(connection) -> list[str]:
    ensure_opencode_model_routes(connection)
    routed = route_capabilities(
        connection, required_actions=["engineering.build.model"], kind="cli",
        max_cost_cents=0, max_privacy_class="internal",
    )
    return [cap["metadata"]["model"] for cap in routed
            if cap["metadata"].get("adapter") == MODEL_ADAPTER
            and cap["metadata"].get("provider") == "opencode"
            and permits_internal_model(cap["metadata"].get("model", ""))
            and cap["name"] == _model_name(cap["metadata"].get("model", ""))]


def record_model_failure(connection, model: str, reason: str) -> None:
    name = _model_name(model)
    ensure_opencode_model_routes(connection)
    previous = next((cap for cap in list_capabilities(connection) if cap["name"] == name), None)
    streak = int((previous or {}).get("metadata", {}).get("circuit_breaker", {}).get("consecutive_failures", 0))
    cooldown = min(1800, 60 * (2 ** min(streak, 4)))
    record_capability_failure(connection, name, error=reason, threshold=1, cooldown_seconds=cooldown)


def record_model_success(connection, model: str, latency_ms: int) -> None:
    name = _model_name(model)
    ensure_opencode_model_routes(connection)
    record_capability_success(connection, name)
    connection.execute("UPDATE capabilities SET latency_ms=? WHERE name=?", (max(0, int(latency_ms)), name))
    connection.commit()


def _ollama_model(*, allow_start: bool) -> str | None:
    from .local_ollama import ready_model
    try:
        return ready_model(allow_start=allow_start)
    except (AdapterError, OSError, TimeoutError):
        return None


def _ollama_ready(*, allow_start: bool) -> bool:
    return _ollama_model(allow_start=allow_start) is not None


def _approved_reenable(connection, current, provider: str, ready: bool) -> int | None:
    if provider != "opencode" or not ready or not current or current["enabled"]:
        return None
    metadata = current.get("metadata", {})
    approval_id = metadata.get("activation_approval_id")
    if type(approval_id) is not int:
        return None
    row = connection.execute(
        "SELECT action,state,cost_cents,payload_json,expires_at FROM approvals WHERE id=?",
        (approval_id,),
    ).fetchone()
    if not row or row["action"] != "ENABLE_ENGINEERING_PROVIDER" or row["state"] != "approved" or row["cost_cents"] != 0:
        return None
    if row["expires_at"]:
        from datetime import datetime, timezone
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            return None
    payload = json.loads(row["payload_json"])
    if payload.get("capability") != "engineering.cli.opencode" or payload.get("cost_cents") != 0:
        return None
    if current["permissions"] != ["isolated_workspace_only"] or current["owner_approval_required"]:
        return None
    if current["cost_fixed_cents"] != 0 or current["auth_status"] != "ready":
        return None
    return approval_id

def probe(connection, repo: Path, *, force=False):
    from .engineering import _provider_ready, ai_command
    existing = {cap["name"]: cap for cap in list_capabilities(connection)}
    results = []
    for provider in ADAPTERS:
        name = PREFIX + provider
        previous = existing.get(name)
        metadata = dict(previous["metadata"]) if previous else {}
        if not force and time.time() - metadata.get("checked_epoch", 0) < 300:
            results.append(previous)
            continue
        argv = None if provider == "ollama" else ai_command(provider)
        ready = False
        ollama_model = None
        if provider == "ollama":
            # Starting an already-installed user-level Ollama service is bounded,
            # reversible recovery. This never installs software or downloads a model.
            ollama_model = _ollama_model(allow_start=True)
            ready = ollama_model is not None
        else:
            try:
                ready = bool(argv) and _provider_ready(provider, argv, repo)
            except (AdapterError, OSError, TimeoutError):
                pass
        evidence = None
        if (provider == "codex" and ready and previous and previous["enabled"]
                and not previous["owner_approval_required"] and previous["cost_fixed_cents"] == 0
                and previous["auth_status"] == "ready"):
            from .codex_health import is_quota_failure, read_subscription_status
            circuit = metadata.get("circuit_breaker", {})
            if circuit.get("state") == "open" and is_quota_failure(circuit):
                evidence = read_subscription_status(argv, repo)
        recovered_quota = False
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = next((cap for cap in list_capabilities(connection) if cap["name"] == name), None)
            if ((previous and current is None) or (current and (
                    current["kind"] != "cli" or current["metadata"].get("adapter") != "life_os.engineering.v2"))):
                connection.commit()
                results.append({"name": name, "ready": False, "reason": "route_ownership_changed"})
                continue
            metadata = dict(current["metadata"]) if current else {}
            if evidence is not None:
                metadata["subscription_quota_probe"] = evidence
                if (evidence.get("state") == "available" and current and previous
                        and current == previous
                        and current["enabled"] and not current["owner_approval_required"]
                        and current["cost_fixed_cents"] == 0 and current["auth_status"] == "ready"
                        and metadata.get("circuit_breaker") == previous["metadata"].get("circuit_breaker")):
                    metadata["previous_quota_circuit"] = dict(metadata["circuit_breaker"])
                    metadata["circuit_breaker"] = {"state": "closed", "consecutive_failures": 0,
                                                   "reopen_at": None, "last_error": ""}
                    metadata["execution_health"] = "quota_available_execution_unverified"
                    recovered_quota = True
            if provider == "opencode" and ready and metadata.get("routing_scope") != "model":
                old_circuit = metadata.get("circuit_breaker")
                if old_circuit:
                    metadata["legacy_provider_circuit"] = old_circuit
                metadata["circuit_breaker"] = {"state": "closed", "consecutive_failures": 0,
                                               "reopen_at": None, "last_error": ""}
                metadata["routing_scope"] = "model"
            if provider == "ollama":
                metadata.update({
                    "routing_scope": "local_floor", "model_source": "installed_only",
                    "network_scope": "loopback_only", "cloud_auth_required": False,
                    "resource_policy": "host_memory_aware_load_on_demand_unload_after_turn",
                    "selected_model": ollama_model or "",
                })
            metadata.update({
                "adapter": "life_os.engineering.v2", "provider": provider,
                "role": "subordinate_engineering", "execution_owner": "monolith",
                "checked_epoch": time.time(),
                "probe": "local_model_readiness" if provider == "ollama" else "authentication_only",
                "cost_evidence": "local_zero_marginal_cost" if provider == "ollama" else "not_established_by_probe",
                "execution_health": metadata.get("execution_health", "unverified"),
            })
            reenable_approval = _approved_reenable(connection, current, provider, ready)
            if current:
                fields = {key: current[key] for key in (
                    "enabled", "permissions", "owner_approval_required", "priority", "auth_required",
                    "reliability", "latency_ms", "cost_fixed_cents", "privacy_class", "actions",
                    "reversible", "rate_limit", "failure_mode", "recovery_method",
                )}
                if reenable_approval is not None:
                    fields["enabled"] = True
            elif provider == "ollama":
                fields = {
                    "enabled": True, "permissions": ["isolated_workspace_only"],
                    "auth_required": False, "actions": ["engineering.build"],
                    "owner_approval_required": False, "priority": 10,
                    "privacy_class": "internal", "cost_fixed_cents": 0,
                    "reliability": 0.4, "latency_ms": 0,
                    "reversible": True, "rate_limit": {},
                    "failure_mode": "local_model_or_service_unavailable",
                    "recovery_method": "restart_installed_ollama_then_reprobe",
                }
            else:
                fields = {
                    "enabled": bool(existing.get("ai.cli." + provider, {}).get("enabled", provider == "codex")),
                    "permissions": ["isolated_workspace_only"], "auth_required": True,
                    "actions": ["engineering.build"], "owner_approval_required": False,
                    "priority": 80, "privacy_class": "internal",
                }
            if provider == "ollama":
                auth_status = "ready"
            else:
                auth_status = current["auth_status"] if current and current["auth_status"] in {"blocked", "missing"} else (
                    "ready" if ready else "unknown")
            health = "degraded" if metadata.get("circuit_breaker", {}).get("state") == "open" else "healthy"
            upsert_capability(connection, name=name, kind="cli", health=health if ready else "unavailable",
                              auth_status=auth_status, metadata=metadata, **fields)
        except BaseException:
            connection.rollback()
            raise
        if reenable_approval is not None:
            append_event(connection, "engineering.provider.reenabled_from_existing_authority", {
                "name": name, "approval_id": reenable_approval,
                "scope": "existing zero-cost isolated engineering authority",
            })
        if recovered_quota:
            append_event(connection, "engineering.provider.quota_recovered", {
                "name": name, "evidence": evidence,
                "authority": "existing enabled zero-cost subscription route",
            })
        if provider == "opencode" and ready:
            ensure_opencode_model_routes(connection)
        results.append({"name": name, "ready": ready})
    return results


def select_builder(connection, repo: Path, *, exclude=()):
    from .engineering import ai_command
    probe(connection, repo)
    excluded = set(exclude)
    for cap in route_capabilities(connection, required_actions=["engineering.build"], kind="cli"):
        provider = cap["metadata"].get("provider")
        if provider not in ADAPTERS or provider in excluded:
            continue
        if cap["metadata"].get("adapter") != "life_os.engineering.v2":
            continue
        if cap["cost_fixed_cents"] != 0:
            continue
        if provider == "opencode" and not eligible_opencode_models(connection):
            continue
        if provider == "ollama":
            model = _ollama_model(allow_start=True)
            if model:
                return provider, [model]
            continue
        argv = ai_command(provider)
        if argv:
            return provider, argv
    raise CapabilityUnavailable("No ready authorized engineering adapter; bounded provider recovery exhausted")


def record_failure(connection, provider, reason):
    lowered = (reason or "").lower()
    if "produced no changes" in lowered:
        append_event(connection, "engineering.provider.objective_no_change", {
            "provider": provider, "reason": reason[:500],
        })
        return
    if provider == "opencode" and "model routes exhausted" in lowered:
        append_event(connection, "engineering.provider.model_routes_exhausted", {
            "provider": provider, "reason": reason[:500],
        })
        return
    name = PREFIX + provider
    if provider == "ollama":
        ready = _ollama_ready(allow_start=True)
        append_event(connection, "engineering.provider.local_floor_failure", {
            "provider": provider, "reason": reason[:500], "ready_after_recovery": ready,
        })
        if ready:
            record_capability_success(connection, name)
            return
        record_capability_failure(connection, name, error=reason, threshold=1, cooldown_seconds=15)
        return
    previous = next((cap for cap in list_capabilities(connection) if cap["name"] == name), None)
    streak = int((previous or {}).get("metadata", {}).get("circuit_breaker", {}).get("consecutive_failures", 0))
    if any(token in lowered for token in ("usage-limit", "usage limit", "quota", "try again at")):
        cooldown = 6 * 3600
    else:
        cooldown = min(1800, 30 * (2 ** min(streak, 6)))
    record_capability_failure(connection, name, error=reason, threshold=1, cooldown_seconds=cooldown)


def record_success(connection, provider):
    name = PREFIX + provider
    if not record_capability_success(connection, name):
        return
    row = connection.execute("SELECT metadata_json FROM capabilities WHERE name=?", (name,)).fetchone()
    metadata = json.loads(row["metadata_json"])
    metadata["execution_health"] = "host_verified"
    metadata["verified_epoch"] = time.time()
    connection.execute("UPDATE capabilities SET metadata_json=? WHERE name=?", (json.dumps(metadata), name))
    connection.commit()


def _install_engineering_provider_floor_bridge() -> None:
    """Preserve the local provider floor across the objective-lineage rebase.

    This compatibility bridge is intentionally tiny and removable once the next
    engineering.py refactor naturally contains these hooks again. It patches only
    the three provider extension points added by the already-merged provider-floor
    change; routing/audit identity remains `ollama`.
    """
    from . import engineering
    if getattr(engineering, "_provider_floor_bridge_v1", False):
        return
    original_ready = engineering._provider_ready
    original_select = engineering._select_builder
    original_invoke = engineering._invoke_builder

    def provider_ready(provider, argv, repo):
        if provider == "ollama":
            return _ollama_ready(allow_start=True)
        return original_ready(provider, argv, repo)

    def select(repo, *, exclude=(), connection=None):
        if connection is not None:
            return original_select(repo, exclude=exclude, connection=connection)
        try:
            return original_select(repo, exclude=exclude, connection=None)
        except CapabilityUnavailable:
            if "ollama" in set(exclude):
                raise
            model = _ollama_model(allow_start=True)
            if not model:
                raise
            return "ollama", [model]

    def invoke(provider, argv, worktree, prompt, *, pulse=None, connection=None):
        if provider == "ollama":
            from .local_ollama import invoke as invoke_local_ollama
            model = str(argv[0]) if argv else None
            invoke_local_ollama(worktree, prompt, model=model, pulse=pulse)
            return
        return original_invoke(provider, argv, worktree, prompt, pulse=pulse, connection=connection)

    engineering._provider_ready = provider_ready
    engineering._select_builder = select
    engineering._invoke_builder = invoke
    engineering._provider_floor_bridge_v1 = True


_install_engineering_provider_floor_bridge()
