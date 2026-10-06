"""Evidence-based operational invariant supervisor.

This is the layer that notices failures before an operator has to.  It does not
pretend impossible states are healthy: it attempts bounded deterministic repair,
records evidence, and only escalates after recovery is exhausted.  Persistent
code-fixable failures can create one bounded internal engineering objective; the
normal promotion/approval gates still govern external GitHub changes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .attention import emit_attention
from .events import append_event
from .queue import get_state, set_state

STATE_KEY = "operational.invariants"
SCAN_VERSION = 1
GUARDIAN_STALE_SECONDS = 180
NATIVE_CONTROL_STALE_SECONDS = 30
DELIVERY_STALL_SECONDS = 15 * 60
ESCALATE_AFTER = 3
REPAIR_COOLDOWN_SECONDS = 6 * 60 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(value: str | None, *, now_epoch: float) -> float | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, now_epoch - when)


def _load_previous(connection: sqlite3.Connection) -> dict[str, Any]:
    raw = get_state(connection, STATE_KEY)
    if not raw:
        return {"version": SCAN_VERSION, "invariants": {}, "repairs": {}}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"version": SCAN_VERSION, "invariants": {}, "repairs": {}}
    if not isinstance(value, dict) or value.get("version") != SCAN_VERSION:
        return {"version": SCAN_VERSION, "invariants": {}, "repairs": {}}
    if not isinstance(value.get("invariants"), dict):
        value["invariants"] = {}
    if not isinstance(value.get("repairs"), dict):
        value["repairs"] = {}
    return value


def _ready_engineering_providers(connection: sqlite3.Connection) -> list[str]:
    from .capabilities import route_capabilities
    from .engineering_providers import eligible_opencode_models

    routed = route_capabilities(
        connection,
        required_actions=["engineering.build"],
        kind="cli",
        max_cost_cents=0,
        max_privacy_class="internal",
    )
    result: list[str] = []
    for capability in routed:
        metadata = capability.get("metadata", {})
        if metadata.get("adapter") != "life_os.engineering.v2":
            continue
        provider = str(metadata.get("provider") or "")
        if provider == "opencode" and not eligible_opencode_models(connection):
            continue
        if provider:
            result.append(provider)
    return sorted(set(result))


def _provider_invariant(connection: sqlite3.Connection, repo: Path) -> dict[str, Any]:
    ready = _ready_engineering_providers(connection)
    attempted_recovery = False
    if not ready:
        attempted_recovery = True
        try:
            from .engineering_providers import probe
            probe(connection, repo, force=True)
        except Exception as exc:  # evidence only; do not let one probe kill maintenance
            probe_error = type(exc).__name__
        else:
            probe_error = ""
        ready = _ready_engineering_providers(connection)
    else:
        probe_error = ""
    return {
        "healthy": bool(ready),
        "ready_providers": ready,
        "recovery_attempted": attempted_recovery,
        "probe_error": probe_error,
        "code_fixable": False,
        "reason": "" if ready else "no ready zero-cost engineering provider after bounded recovery",
    }


def _native_control_state(connection: sqlite3.Connection, *, now_epoch: float) -> dict[str, Any]:
    raw=get_state(connection,"windows_control.last_status")
    if not raw:
        return {"healthy":False,"status":"missing","age_seconds":None,"reason":"native Windows control has no health receipt","code_fixable":True}
    try:
        value=json.loads(raw)
    except (TypeError,json.JSONDecodeError):
        return {"healthy":False,"status":"invalid","age_seconds":None,"reason":"native Windows control health receipt is invalid","code_fixable":True}
    stamp=value.get("observed_at_epoch") if isinstance(value,dict) else None
    age=max(0.0,now_epoch-float(stamp)) if isinstance(stamp,(int,float)) and stamp<=now_epoch else None
    reachable=bool(value.get("reachable")) if isinstance(value,dict) else False
    healthy=reachable and age is not None and age<=NATIVE_CONTROL_STALE_SECONDS
    reason=""
    if not healthy:
        if age is None: reason="native Windows control has no valid timestamp"
        elif age>NATIVE_CONTROL_STALE_SECONDS: reason="native Windows control health receipt is stale"
        else: reason="native Windows control relay is unreachable: "+str(value.get("reason") or "unknown")[:120]
    return {
        "healthy":healthy,
        "status":"online" if healthy else "degraded",
        "age_seconds":None if age is None else round(age,1),
        "reason":reason,
        "code_fixable":True,
        "worker_recovery":value.get("worker_recovery") if isinstance(value,dict) else None,
    }


def _guardian_state(home: Path, *, now_epoch: float) -> dict[str, Any]:
    path = home / "runtime" / "desktop-commander-guardian.json"
    if not path.is_file():
        return {
            "healthy": False,
            "status": "missing",
            "age_seconds": None,
            "reason": "Desktop Commander guardian has no state receipt",
            "code_fixable": True,
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {
            "healthy": False,
            "status": "invalid",
            "age_seconds": None,
            "reason": "Desktop Commander guardian state is unreadable",
            "code_fixable": True,
        }
    timestamp = value.get("timestamp_epoch")
    age = max(0.0, now_epoch - float(timestamp)) if isinstance(timestamp, (int, float)) else None
    status = str(value.get("status") or "unknown")
    healthy = status == "online" and age is not None and age <= GUARDIAN_STALE_SECONDS
    reason = ""
    if not healthy:
        if age is None:
            reason = "Desktop Commander guardian state has no valid timestamp"
        elif age > GUARDIAN_STALE_SECONDS:
            reason = "Desktop Commander guardian state is stale"
        else:
            reason = f"Desktop Commander guardian is {status}"
    return {
        "healthy": healthy,
        "status": status,
        "age_seconds": None if age is None else round(age, 1),
        "reason": reason,
        "code_fixable": True,
    }


def _start_guardian(home: Path, repo: Path) -> bool:
    """Attempt only the repo-owned, mutex-protected local guardian."""
    if os.name != "nt":
        return False
    script = repo / "scripts" / "desktop_commander_guardian.ps1"
    if not script.is_file():
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-HomeDir", str(home),
            ],
            cwd=str(repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError:
        return False
    return True


def _delivery_invariant(
    connection: sqlite3.Connection, repo: Path, *, now_epoch: float,
) -> dict[str, Any]:
    from .engineering_delivery import _gh, initialize as initialize_delivery

    initialize_delivery(connection)
    rows = connection.execute(
        """SELECT id,repo_slug,pr_number,head_sha,updated_at
           FROM engineering_promotions
           WHERE state='ci_passed' AND pr_number IS NOT NULL AND repo_slug!=''
           ORDER BY updated_at LIMIT 20"""
    ).fetchall()
    stalled: list[dict[str, Any]] = []
    confirmed_merged = 0
    unknown = 0
    for row in rows:
        age = _age_seconds(row["updated_at"], now_epoch=now_epoch)
        if age is None or age < DELIVERY_STALL_SECONDS:
            continue
        try:
            raw = _gh(
                [
                    "pr", "view", str(row["pr_number"]), "--repo", row["repo_slug"],
                    "--json", "state,mergedAt,headRefOid,url",
                ],
                cwd=repo,
                timeout=45,
            )
            remote = json.loads(raw)
        except Exception:
            unknown += 1
            continue
        state = str(remote.get("state") or "").upper()
        merged = bool(remote.get("mergedAt")) or state == "MERGED"
        head = str(remote.get("headRefOid") or "")
        if merged and (not head or head == row["head_sha"]):
            confirmed_merged += 1
            continue
        stalled.append({
            "promotion_id": int(row["id"]),
            "pr_number": int(row["pr_number"]),
            "repo_slug": row["repo_slug"],
            "age_seconds": round(age, 1),
            "remote_state": state or "UNKNOWN",
            "head_matches": not head or head == row["head_sha"],
        })
    return {
        "healthy": not stalled,
        "stalled": stalled,
        "confirmed_merged": confirmed_merged,
        "remote_unknown": unknown,
        "reason": "" if not stalled else "CI-passed engineering PR remains unmerged beyond delivery SLO",
        "code_fixable": True,
    }


def _update_streak(previous: dict[str, Any], key: str, evidence: dict[str, Any]) -> dict[str, Any]:
    prior = previous.get("invariants", {}).get(key, {})
    streak = 0 if evidence.get("healthy") else int(prior.get("failure_streak", 0)) + 1
    return {
        **evidence,
        "failure_streak": streak,
        "checked_at": _now_iso(),
    }


def _submit_self_repair(
    connection: sqlite3.Connection,
    *,
    home: Path,
    repo: Path,
    key: str,
    evidence: dict[str, Any],
    previous: dict[str, Any],
    now_epoch: float,
) -> dict[str, Any] | None:
    if not evidence.get("code_fixable") or int(evidence.get("failure_streak", 0)) < ESCALATE_AFTER:
        return None
    repairs = previous.setdefault("repairs", {})
    last = repairs.get(key, {})
    last_epoch = float(last.get("submitted_epoch", 0) or 0)
    if last_epoch and now_epoch - last_epoch < REPAIR_COOLDOWN_SECONDS:
        return None
    if not _ready_engineering_providers(connection):
        return None

    bounded = json.dumps(
        {name: value for name, value in evidence.items() if name not in {"checked_at"}},
        separators=(",", ":"), sort_keys=True,
    )[:2000]
    goal = (
        f"Repair persistent operational invariant {key}. Inspect current repository state, identify the root cause, "
        f"make the smallest safe reversible change, and add regression coverage. Runtime evidence: {bounded}"
    )
    acceptance = [
        f"Operational invariant {key} has a deterministic detection and bounded recovery path.",
        "The repair preserves existing approval, security, rollback, audit, and provider-isolation boundaries.",
        "Focused regression coverage proves the observed failure cannot silently recur in the same way.",
    ]
    try:
        from .engineering import submit
        run = submit(
            connection,
            goal=goal,
            acceptance=acceptance,
            home=home,
            repo_root=repo,
            priority=100,
        )
    except Exception as exc:
        append_event(connection, "operational.invariant_repair_submission_failed", {
            "key": key, "error": type(exc).__name__,
        })
        return None
    receipt = {
        "run_id": int(run["id"]),
        "created": bool(run.get("created")),
        "submitted_epoch": now_epoch,
    }
    repairs[key] = receipt
    append_event(connection, "operational.invariant_repair_submitted", {
        "key": key, **receipt,
    })
    return receipt


def scan(
    connection: sqlite3.Connection,
    *,
    home: Path,
    repo: Path | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Check operational truth, attempt bounded recovery, and self-escalate."""
    home = Path(home).expanduser().resolve()
    repo = Path(repo or Path(__file__).resolve().parents[1]).resolve()
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    previous = _load_previous(connection)

    provider = _provider_invariant(connection, repo)
    native_control = _native_control_state(connection, now_epoch=now_epoch)
    delivery = _delivery_invariant(connection, repo, now_epoch=now_epoch)

    evidence = {
        "engineering.provider_continuity": _update_streak(previous, "engineering.provider_continuity", provider),
        "windows_control.native": _update_streak(previous, "windows_control.native", native_control),
        "engineering.verified_delivery": _update_streak(previous, "engineering.verified_delivery", delivery),
    }

    previous["version"] = SCAN_VERSION
    previous["invariants"] = evidence
    previous["checked_at"] = _now_iso()
    repairs: dict[str, Any] = {}
    for key, item in evidence.items():
        if item["healthy"]:
            continue
        repair = _submit_self_repair(
            connection,
            home=home,
            repo=repo,
            key=key,
            evidence=item,
            previous=previous,
            now_epoch=now_epoch,
        )
        if repair is not None:
            repairs[key] = repair
        if int(item.get("failure_streak", 0)) >= ESCALATE_AFTER:
            emit_attention(
                connection,
                fingerprint=f"operational-invariant:{key}:{int(now_epoch // 3600)}",
                kind="failure_unrepaired",
                severity="warning",
                source="life-os.operational-invariants",
                payload={
                    "invariant": key,
                    "reason": str(item.get("reason") or "operational invariant is false")[:700],
                    "failure_streak": item["failure_streak"],
                    "self_repair_submitted": key in repairs,
                },
            )

    set_state(connection, STATE_KEY, json.dumps(previous, separators=(",", ":"), sort_keys=True))
    result = {
        "healthy": all(item["healthy"] for item in evidence.values()),
        "invariants": evidence,
        "repairs_submitted": repairs,
    }
    append_event(connection, "operational.invariants_scanned", {
        "healthy": result["healthy"],
        "failed": [key for key, value in evidence.items() if not value["healthy"]],
        "repairs_submitted": sorted(repairs),
    })
    return result
