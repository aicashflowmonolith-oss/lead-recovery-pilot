"""Outbound, bounded control bridge for LIFE OS.

The client opens no listening socket and offers no shell. It polls MONOLITH for
already-authorized capability-scoped requests, records them in the existing sync
inbox, submits only the bounded request.submit.v1 capability into request_fabric,
and returns a receipt only after LIFE OS reaches a terminal verified state.

The bridge may also recover the local execution plane when it can prove the
normal worker heartbeat is stale and the deterministic recovery controller is
not currently healthy. That recovery is fixed-function only: it can start the
same LIFE OS worker executable against the same durable database, never an
arbitrary command supplied by the network.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import uuid

from .engine_bridge import EliteSystemBrain
from .principal_agent import CONSTITUTION_HASH, PRINCIPAL_ID
from .queue import get_state, set_state, stats
from .sync import acknowledge, emit, ingest, mark_processed


BRIDGE_VERSION = 1
CAPABILITY = "request.submit.v1"
DEFAULT_URL = "https://monolith-new-production.up.railway.app"
MAX_RESPONSE_BYTES = 65536
TERMINAL_LOCAL_STATES = {"succeeded", "failed", "cancelled"}
WORKER_STALE_SECONDS = 120
RECOVERY_CONTROLLER_FRESH_SECONDS = 180
WORKER_RECOVERY_COOLDOWN_SECONDS = 120
WORKER_RECOVERY_ATTEMPT_KEY = "control_bridge.worker_recovery.last_attempt_epoch"
WORKER_RECOVERY_RECEIPT_KEY = "control_bridge.worker_recovery.last_receipt"


class BridgeUnavailable(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return value
    raise ValueError("control bridge URL must use HTTPS")


def _post_json(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    token: str,
    timeout: float = 4.0,
) -> dict[str, Any]:
    if not 32 <= len(token) <= 512:
        raise ValueError("invalid control bridge credential")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("control bridge request exceeds boundary")
    request = Request(
        urljoin(_base_url(base_url) + "/", path.lstrip("/")),
        data=encoded,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise BridgeUnavailable(f"control bridge HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise BridgeUnavailable("control bridge network unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BridgeUnavailable("control bridge response exceeds boundary")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BridgeUnavailable("control bridge returned invalid JSON") from None
    if not isinstance(value, dict):
        raise BridgeUnavailable("control bridge returned invalid envelope")
    return value


def _machine_id(connection: sqlite3.Connection) -> str:
    key = "control_bridge.machine_id"
    existing = get_state(connection, key)
    if isinstance(existing, str) and 1 <= len(existing) <= 128:
        return existing
    value = "life-os-" + uuid.uuid4().hex
    set_state(connection, key, value)
    return value


def _state_dict(connection: sqlite3.Connection, key: str) -> dict[str, Any]:
    raw = get_state(connection, key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp_age(value: dict[str, Any], *, now: float | None = None) -> float | None:
    stamp = value.get("timestamp_epoch")
    now = time.time() if now is None else now
    if type(stamp) not in (int, float) or stamp > now:
        return None
    return max(0.0, now - float(stamp))


def _heartbeat(connection: sqlite3.Connection) -> dict[str, Any]:
    worker = _state_dict(connection, "worker.heartbeat")
    age = _timestamp_age(worker)
    return {
        "bridge_version": BRIDGE_VERSION,
        "runtime_fingerprint": worker.get("runtime_fingerprint"),
        "worker_health": "fresh" if age is not None and age <= WORKER_STALE_SECONDS else "stale_or_unknown",
        "worker_heartbeat_age_seconds": None if age is None else round(age, 3),
        "queue": stats(connection),
        "network_state": "outbound_poll",
        "last_control_exchange": get_state(connection, "control_bridge.last_exchange"),
    }


def _database_path(connection: sqlite3.Connection) -> Path | None:
    for row in connection.execute("PRAGMA database_list"):
        if row[1] != "main" or not row[2]:
            continue
        path = Path(row[2]).expanduser().resolve()
        return path if path.is_file() else None
    return None


def _record_recovery(connection: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
    set_state(connection, WORKER_RECOVERY_RECEIPT_KEY, json.dumps(value, sort_keys=True))
    return value


def _maybe_recover_worker(
    connection: sqlite3.Connection,
    *,
    now: float | None = None,
    popen: Any = subprocess.Popen,
) -> dict[str, Any]:
    """Start only the canonical local worker when the normal recovery owner is stale.

    This is intentionally not a general process launcher. The executable, module,
    database, home, lane and arguments are all derived locally and fixed here.
    InstanceLock in the worker remains the final duplicate-worker guard.
    """
    now = time.time() if now is None else float(now)
    if _local_stopped(connection):
        return {"attempted": False, "reason": "locally_stopped"}

    worker_age = _timestamp_age(_state_dict(connection, "worker.heartbeat"), now=now)
    if worker_age is not None and worker_age <= WORKER_STALE_SECONDS:
        return {"attempted": False, "reason": "worker_fresh"}

    controller_age = _timestamp_age(_state_dict(connection, "recovery.status"), now=now)
    if controller_age is not None and controller_age <= RECOVERY_CONTROLLER_FRESH_SECONDS:
        return {"attempted": False, "reason": "recovery_controller_fresh"}

    try:
        last_attempt = float(get_state(connection, WORKER_RECOVERY_ATTEMPT_KEY) or "0")
    except (TypeError, ValueError):
        last_attempt = 0.0
    if last_attempt and now - last_attempt < WORKER_RECOVERY_COOLDOWN_SECONDS:
        return {"attempted": False, "reason": "cooldown"}

    database = _database_path(connection)
    if database is None:
        return _record_recovery(connection, {
            "attempted": False,
            "reason": "durable_database_unavailable",
            "timestamp_epoch": now,
        })

    set_state(connection, WORKER_RECOVERY_ATTEMPT_KEY, str(now))
    home = database.parent
    backups = home / "backups"
    log_dir = home / "logs"
    backups.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    worker_log = log_dir / "worker-bridge-recovery.log"
    runtime_root = Path(__file__).resolve().parents[1]
    argv = [
        sys.executable,
        "-m",
        "life_os",
        "--db",
        str(database),
        "worker",
        "--home",
        str(home),
        "--backups",
        str(backups),
        "--log",
        str(worker_log),
    ]
    env = os.environ.copy()
    env["LIFE_OS_WORKER_LANE"] = "all"
    env.pop("LIFE_OS_WORKER_START_GATE", None)
    env.pop("LIFE_OS_RECOVERY_SESSION", None)
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        child = popen(
            argv,
            cwd=str(runtime_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        return _record_recovery(connection, {
            "attempted": True,
            "started": False,
            "reason": type(exc).__name__,
            "timestamp_epoch": now,
        })
    return _record_recovery(connection, {
        "attempted": True,
        "started": True,
        "pid": int(child.pid),
        "lane": "all",
        "reason": "stale_worker_no_fresh_recovery_controller",
        "timestamp_epoch": now,
    })


def _try_inline_quick(
    connection: sqlite3.Connection,
    *,
    text: str,
    execution_request_id: str,
) -> bool:
    """Keep deterministic local notes/tasks available while the worker recovers.

    Only the pre-existing QUICK grammar is eligible. No model, shell, network
    side effect or arbitrary operation is executed here. The durable queue row is
    intentionally retained; a later worker pass observes the terminal request and
    completes that queue job idempotently.
    """
    from . import request_fabric

    if not request_fabric.QUICK.match(text):
        return False
    if _heartbeat(connection)["worker_health"] == "fresh":
        return False
    try:
        request_fabric.complete_quick_command(connection, execution_request_id)
    except Exception as exc:
        set_state(connection, "control_bridge.inline_quick.last_status", json.dumps({
            "completed": False,
            "reason": type(exc).__name__,
            "timestamp": _now(),
        }, sort_keys=True))
        return False
    set_state(connection, "control_bridge.inline_quick.last_status", json.dumps({
        "completed": True,
        "timestamp": _now(),
    }, sort_keys=True))
    return True


def _validate_principal_metadata(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid principal metadata")
    required = {
        "id",
        "decision_id",
        "constitution_version",
        "constitution_hash",
        "action_class",
        "authority",
        "risk",
        "requires_human",
        "world_state_fingerprint",
    }
    if set(value) != required:
        raise ValueError("invalid principal metadata fields")
    if value["id"] != PRINCIPAL_ID or value["constitution_hash"] != CONSTITUTION_HASH:
        raise ValueError("principal governance contract mismatch")
    if value["authority"] not in {"AUTO", "POLICY"} or value["requires_human"] is not False:
        raise ValueError("non-delegable principal decision cannot cross execution bridge")
    for key in (
        "decision_id",
        "constitution_version",
        "action_class",
        "risk",
        "world_state_fingerprint",
    ):
        item = value[key]
        if not isinstance(item, str) or not 1 <= len(item) <= 128:
            raise ValueError(f"invalid principal metadata {key}")
    return value


def _validate_request(value: Any, machine_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid control request")
    required = {
        "request_id",
        "schema_version",
        "capability",
        "payload",
        "created_at",
        "expires_at",
        "status",
        "claimed_by",
        "claimed_at",
        "revoked_at",
    }
    if set(value) != required:
        raise ValueError("invalid control request fields")
    if value["schema_version"] != 1 or value["capability"] != CAPABILITY:
        raise ValueError("unsupported control request capability")
    request_id = value["request_id"]
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError("invalid control request id")
    if value["status"] != "claimed" or value["claimed_by"] != machine_id:
        raise ValueError("control request is not claimed by this machine")
    expires = datetime.fromisoformat(value["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValueError("control request expired")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise ValueError("invalid control request payload")
    allowed = {"text", "side_effect_owner", "expected_state", "policy_ref", "approval_ref", "principal_agent"}
    if not set(payload) <= allowed or "text" not in payload:
        raise ValueError("unsupported control request payload")
    text = payload["text"]
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
        raise ValueError("control request text is outside bounds")
    if payload.get("side_effect_owner", "life-os") != "life-os":
        raise ValueError("control request has invalid side-effect owner")
    _validate_principal_metadata(payload.get("principal_agent"))
    for key in ("expected_state", "policy_ref", "approval_ref"):
        item = payload.get(key)
        if item is not None and (not isinstance(item, str) or len(item) > 200):
            raise ValueError(f"invalid {key}")
    return value


def _local_stopped(connection: sqlite3.Connection) -> bool:
    return any(
        get_state(connection, key) == "1"
        for key in ("worker.paused", "worker.emergency_stop", "safe_mode.paused")
    )


def _terminal_receipt(
    connection: sqlite3.Connection, request_id: str, execution_request_id: str
) -> tuple[str, dict[str, Any]] | None:
    row = connection.execute(
        "SELECT state,provider,generation FROM execution_requests WHERE id=?",
        (execution_request_id,),
    ).fetchone()
    if row is None or row["state"] not in TERMINAL_LOCAL_STATES:
        return None
    state = row["state"]
    outcome = "succeeded" if state == "succeeded" else "cancelled" if state == "cancelled" else "failed"
    return outcome, {
        "execution_request_id": execution_request_id,
        "state": state,
        "provider": row["provider"],
        "generation": row["generation"],
        "verified": state == "succeeded",
    }


def poll_once(
    connection: sqlite3.Connection,
    *,
    base_url: str | None = None,
    token: str | None = None,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Perform one bounded outbound control exchange without raising on outages."""
    base_url = base_url or os.environ.get("LIFE_OS_CONTROL_BRIDGE_URL", DEFAULT_URL)
    token = token if token is not None else os.environ.get("LIFE_OS_CONTROL_BRIDGE_TOKEN", "")
    if not isinstance(token, str) or not 32 <= len(token.strip()) <= 512:
        result = {"enabled": False, "reachable": False, "reason": "credential_unconfigured"}
        set_state(connection, "control_bridge.last_status", json.dumps(result, sort_keys=True))
        return result
    token = token.strip()
    machine_id = _machine_id(connection)
    try:
        _maybe_recover_worker(connection)
        _post_json(
            base_url,
            "/control/heartbeat",
            {"client_id": machine_id, "payload": _heartbeat(connection)},
            token=token,
            timeout=timeout,
        )
        claimed = _post_json(
            base_url,
            "/control/claim",
            {"client_id": machine_id, "capabilities": [CAPABILITY]},
            token=token,
            timeout=timeout,
        ).get("request")
        if claimed is None:
            stamp = _now()
            set_state(connection, "control_bridge.last_exchange", stamp)
            result = {"enabled": True, "reachable": True, "claimed": False}
            set_state(connection, "control_bridge.last_status", json.dumps(result, sort_keys=True))
            return result

        request = _validate_request(claimed, machine_id)
        request_id = request["request_id"]
        checked = _post_json(
            base_url,
            "/control/check",
            {"client_id": machine_id, "request_id": request_id},
            token=token,
            timeout=timeout,
        ).get("request")
        checked = _validate_request(checked, machine_id)
        if _local_stopped(connection):
            result = {"enabled": True, "reachable": True, "claimed": True, "state": "locally_stopped"}
            set_state(connection, "control_bridge.last_status", json.dumps(result, sort_keys=True))
            return result

        payload = checked["payload"]
        ingest(
            connection,
            event_id=request_id,
            schema_version="monolith.control.v1",
            source="monolith-control",
            target="life-os",
            kind="control.request",
            payload={
                "capability": CAPABILITY,
                "text": payload["text"],
                "side_effect_owner": "life-os",
                "expected_state": payload.get("expected_state"),
                "policy_ref": payload.get("policy_ref"),
                "approval_ref": payload.get("approval_ref"),
            },
            correlation_id=request_id,
        )
        brain = EliteSystemBrain(connection)
        local = brain.submit(payload["text"], idempotency_key=request_id)
        mark_processed(connection, request_id)
        execution_request_id = local["id"]
        terminal = _terminal_receipt(connection, request_id, execution_request_id)
        if terminal is None and _try_inline_quick(
            connection,
            text=payload["text"],
            execution_request_id=execution_request_id,
        ):
            terminal = _terminal_receipt(connection, request_id, execution_request_id)
        if terminal is None:
            result = {
                "enabled": True,
                "reachable": True,
                "claimed": True,
                "request_id": request_id,
                "execution_request_id": execution_request_id,
                "state": local["state"],
            }
            set_state(connection, "control_bridge.last_exchange", _now())
            set_state(connection, "control_bridge.last_status", json.dumps(result, sort_keys=True))
            return result

        outcome, receipt_payload = terminal
        receipt_event_id = "control-receipt-" + execution_request_id
        emit(
            connection,
            target="monolith-control",
            kind="control.receipt",
            payload={"request_id": request_id, "outcome": outcome, **receipt_payload},
            event_id=receipt_event_id,
            correlation_id=request_id,
        )
        _post_json(
            base_url,
            "/control/receipt",
            {
                "client_id": machine_id,
                "request_id": request_id,
                "outcome": outcome,
                "result": receipt_payload,
            },
            token=token,
            timeout=timeout,
        )
        acknowledge(connection, receipt_event_id)
        stamp = _now()
        set_state(connection, "control_bridge.last_exchange", stamp)
        result = {
            "enabled": True,
            "reachable": True,
            "claimed": True,
            "request_id": request_id,
            "execution_request_id": execution_request_id,
            "state": outcome,
            "receipt_acked": True,
        }
        set_state(connection, "control_bridge.last_status", json.dumps(result, sort_keys=True))
        return result
    except (BridgeUnavailable, ValueError) as exc:
        result = {
            "enabled": True,
            "reachable": False,
            "reason": type(exc).__name__,
        }
        set_state(connection, "control_bridge.last_status", json.dumps(result, sort_keys=True))
        return result
