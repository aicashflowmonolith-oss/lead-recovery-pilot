"""Deterministic restoration of MONOLITH's zero-cost local engineering floor.

The light scheduler only enqueues a fixed repair. The existing capability.build
heavy lane owns execution, retries, heartbeats and process containment. No model
output selects commands, models, permissions, credentials, spending, or network
authority. Provisioning is limited to `ollama pull` for the host-aware model
already fixed by local_ollama.model_candidates().
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .ai_cli import CapabilityUnavailable, run_bounded
from .events import append_event
from .queue import enqueue, get_state, set_state

JOB_MARKER = "provider_floor"
STATE_KEY = "engineering.provider_floor.last_schedule_epoch"
SCHEDULE_INTERVAL_SECONDS = 60
FINGERPRINT_BUCKET_SECONDS = 300
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
PULL_TIMEOUT_SECONDS = 15 * 60


def _repo() -> Path:
    return Path(__file__).resolve().parents[1]


def _stopped(connection) -> bool:
    return any(get_state(connection, key) == "1" for key in
               ("worker.paused", "worker.emergency_stop", "safe_mode.paused"))


def _waiting_count(connection) -> int:
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM engineering_runs WHERE state='waiting_provider'"
        ).fetchone()
    except Exception:
        return 0
    return int(row[0]) if row else 0


def _provider_ready(connection) -> bool:
    from .engineering_providers import select_builder
    try:
        select_builder(connection, _repo())
    except CapabilityUnavailable:
        return False
    return True


def schedule(connection, *, now_epoch: float | None = None) -> int:
    """Queue one bounded heavy-lane floor repair when engineering is provider-starved."""
    if _stopped(connection) or _waiting_count(connection) == 0:
        return 0
    active = connection.execute(
        """SELECT 1 FROM worker_jobs
           WHERE kind='capability.build' AND state IN ('queued','running','retry')
             AND json_extract(payload_json,'$.provider_floor')=1 LIMIT 1"""
    ).fetchone()
    if active:
        return 0
    now = time.time() if now_epoch is None else float(now_epoch)
    try:
        previous = float(get_state(connection, STATE_KEY) or 0)
    except ValueError:
        previous = 0.0
    if previous and now - previous < SCHEDULE_INTERVAL_SECONDS:
        return 0
    if _provider_ready(connection):
        set_state(connection, STATE_KEY, str(now))
        return 0
    set_state(connection, STATE_KEY, str(now))
    bucket = int(now // FINGERPRINT_BUCKET_SECONDS)
    _job, created = enqueue(
        connection,
        fingerprint=f"provider.floor:{bucket}",
        kind="capability.build",
        payload={JOB_MARKER: True, "version": 1},
        priority=100,
        max_attempts=5,
    )
    if created:
        append_event(connection, "engineering.provider_floor.repair_queued", {
            "waiting_provider": _waiting_count(connection),
            "execution_lane": "capability.build",
            "authority": "fixed_zero_cost_local_ollama_recipe",
        })
    return int(created)


def execute(connection, *, home: Path, pulse=None) -> dict:
    """Restore and verify one installed Ollama model, then wake waiting engineering."""
    if _stopped(connection):
        return {"state": "paused"}
    if _provider_ready(connection):
        return {"state": "provider_already_ready"}

    from . import local_ollama
    command = local_ollama._ollama_command()
    if not command:
        raise CapabilityUnavailable("Local Ollama executable is unavailable")

    # Start/recover the already-installed loopback service before pulling.
    local_ollama._models_with_recovery(allow_start=True)
    model = local_ollama.configured_model()
    if local_ollama.ensure_ready(model=model, allow_start=True):
        return {"state": "provider_already_ready", "model": model}

    home = Path(home).resolve()
    free = shutil.disk_usage(home).free
    if free < MIN_FREE_BYTES:
        raise CapabilityUnavailable("Insufficient free disk for bounded local provider restoration")

    if pulse:
        pulse()
    code, _out, _err = run_bounded(
        [command, "pull", model],
        cwd=_repo(),
        timeout=PULL_TIMEOUT_SECONDS,
        pulse=pulse,
        max_output_bytes=128 * 1024,
        first_output_timeout=90,
    )
    if code:
        raise CapabilityUnavailable(f"Local Ollama model restoration failed with exit {code}")
    if not local_ollama.ensure_ready(model=model, allow_start=True):
        raise CapabilityUnavailable("Local Ollama model pull completed without verified readiness")

    from .engineering_providers import probe, select_builder
    probe(connection, _repo(), force=True)
    provider, argv = select_builder(connection, _repo())
    if not provider:
        raise CapabilityUnavailable("Provider floor restored but engineering route did not verify")

    from . import engineering
    resumed = engineering.schedule_waiting(connection, allow_probe=False)
    receipt = {
        "state": "restored",
        "provider": provider,
        "model": model if provider == "ollama" else str(argv[0] if argv else ""),
        "waiting_runs_rescheduled": int(resumed),
        "free_bytes_after": shutil.disk_usage(home).free,
    }
    append_event(connection, "engineering.provider_floor.restored", receipt)
    return receipt
