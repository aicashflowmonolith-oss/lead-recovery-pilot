"""Local engineering provider-pool convergence.

Keep MONOLITH from treating a usable installed Ollama model as "no provider"
just because its name is not one of the preferred defaults. The pool is local,
zero-marginal-cost, loopback-only, and inherits local_ollama's isolated
worktree boundary. Missing local models are restored through the existing
capability.build heavy lane, never by blocking the light control worker.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .ai_cli import CapabilityUnavailable
from . import local_ollama
from .queue import get_state


def _base_invoke(worktree: Path, prompt: str, *, model: str | None = None,
                 pulse: Callable[[], None] | None = None) -> None:
    """Call the current bounded base adapter so test/extension seams stay intact."""
    local_ollama.invoke(worktree, prompt, model=model, pulse=pulse)


def ready_models(*, allow_start: bool = True) -> tuple[str, ...]:
    """Return every installed local model, with resource preferences first."""
    installed = tuple(dict.fromkeys(local_ollama._models_with_recovery(allow_start=allow_start)))
    if not installed:
        return ()
    installed_set = set(installed)
    preferred = [model for model in local_ollama.model_candidates() if model in installed_set]
    preferred_set = set(preferred)
    extras = sorted(model for model in installed if model not in preferred_set)
    return tuple(preferred + extras)


def ready_model(*, allow_start: bool = True) -> str | None:
    # Preserve the base adapter's configured-model seam first; expand only when
    # that route is genuinely unavailable.
    base = local_ollama.ready_model(allow_start=allow_start)
    if base:
        return base
    models = ready_models(allow_start=allow_start)
    return models[0] if models else None


def ensure_ready(*, model: str | None = None, allow_start: bool = True) -> bool:
    if model is not None:
        return local_ollama.ensure_ready(model=model, allow_start=allow_start)
    return ready_model(allow_start=allow_start) is not None


def invoke(
    worktree: Path,
    prompt: str,
    *,
    model: str | None = None,
    pulse: Callable[[], None] | None = None,
) -> None:
    """Try the selected route, then every other installed local model."""
    models = list(ready_models(allow_start=True))
    if model:
        if model in models:
            models.remove(model)
        models.insert(0, model)
    if not models:
        raise CapabilityUnavailable("Local Ollama provider pool has no installed models")

    failures: list[str] = []
    for candidate in models:
        try:
            _base_invoke(worktree, prompt, model=candidate, pulse=pulse)
            return
        except CapabilityUnavailable as exc:
            failures.append(f"{candidate}:{type(exc).__name__}")
    raise CapabilityUnavailable(
        f"Local Ollama provider pool exhausted after {len(failures)} local model route(s)"
    )


def _live_worker(connection, *, max_age_seconds: float = 120.0) -> bool:
    """Require fresh runtime evidence before scheduling host-level floor repair."""
    raw = get_state(connection, "worker.heartbeat")
    if not raw:
        return False
    try:
        payload = json.loads(raw)
        observed = float(payload.get("timestamp_epoch", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    age = time.time() - observed
    return 0 <= age <= max_age_seconds


def install() -> None:
    """Install engineering routing plus durable provider-floor recovery bridges."""
    from . import engineering, engineering_providers, request_fabric
    from . import provider_floor

    if getattr(engineering, "_provider_pool_bridge_v1", False):
        return

    original_invoke = engineering._invoke_builder
    original_schedule_waiting = engineering.schedule_waiting
    original_build_capability = request_fabric.build_capability

    def pooled_model(*, allow_start: bool) -> str | None:
        return ready_model(allow_start=allow_start)

    def pooled_ready(*, allow_start: bool) -> bool:
        return ensure_ready(allow_start=allow_start)

    def invoke_builder(provider, argv, worktree, prompt, *, pulse=None, connection=None):
        if provider == "ollama":
            model = str(argv[0]) if argv else None
            invoke(worktree, prompt, model=model, pulse=pulse)
            return
        return original_invoke(
            provider, argv, worktree, prompt, pulse=pulse, connection=connection
        )

    def schedule_waiting(connection, *, interval_seconds=30, allow_probe=True):
        # Preserve the established API and semantics. Floor restoration is an
        # additional live-runtime side effect only when a fresh worker heartbeat
        # proves this is an active host, not a library/disposable-db call.
        created = original_schedule_waiting(
            connection, interval_seconds=interval_seconds, allow_probe=allow_probe
        )
        if _live_worker(connection):
            provider_floor.schedule(connection)
        return created

    def build_capability(connection, job, *, home=None, pulse=None, repo_root=None):
        if job.payload.get(provider_floor.JOB_MARKER) is True:
            if home is None:
                database = next((row[2] for row in connection.execute("PRAGMA database_list")
                                 if row[1] == "main"), "")
                if not database:
                    raise ValueError("Provider-floor repair requires a durable local database")
                repair_home = Path(database).resolve().parent
            else:
                repair_home = Path(home).resolve()
            return provider_floor.execute(connection, home=repair_home, pulse=pulse)
        return original_build_capability(
            connection, job, home=home, pulse=pulse, repo_root=repo_root
        )

    # engineering_providers resolves these globals at call time, so its existing
    # probe/select/failure paths gain the pool without changing base adapter semantics.
    engineering_providers._ollama_model = pooled_model
    engineering_providers._ollama_ready = pooled_ready
    engineering._invoke_builder = invoke_builder
    engineering.schedule_waiting = schedule_waiting
    request_fabric.build_capability = build_capability
    engineering._provider_pool_bridge_v1 = True
