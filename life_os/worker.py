"""Persistent autonomous runtime for LIFE OS."""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sqlite3
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from .queue import (
    Job,
    claim_next,
    complete,
    enqueue,
    enqueue_if_kind_idle,
    fail,
    get_state,
    initialize_queue,
    recover_orphaned,
    converge_periodic_jobs,
    set_state,
    stats,
)
from . import roles
from .owned_processes import ProcessContainmentError

LOG = logging.getLogger("life_os.worker")

class AlreadyRunningError(RuntimeError):
    pass

class InstanceLock:
    """Cross-platform non-blocking singleton lock held for the worker lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunningError("LIFE OS worker is already running") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    if LOG.handlers:
        return
    handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    LOG.addHandler(handler)

class Worker:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        home: Path,
        backups: Path,
        poll_seconds: float = 3.0,
        lease_seconds: int = 120,
        coordinator_scan_seconds: int = 900,
        maintainer_scan_seconds: int = 3600,
        performance_scan_seconds: int = 300,
        reality_scan_seconds: int = 60,
        worker_id: str | None = None,
    ):
        self.connection = connection
        self.home = home
        self.backups = backups
        self.poll_seconds = max(0.1, poll_seconds)
        self.lease_seconds = max(300, lease_seconds)
        self.coordinator_scan_seconds = max(60, coordinator_scan_seconds)
        self.maintainer_scan_seconds = max(60, maintainer_scan_seconds)
        self.performance_scan_seconds = max(60, performance_scan_seconds)
        self.reality_scan_seconds = max(30, reality_scan_seconds)
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.lane = os.environ.get("LIFE_OS_WORKER_LANE", "all")
        if self.lane not in {"all", "light", "heavy"}:
            raise ValueError("invalid execution lane")
        recovery_session = os.environ.get("LIFE_OS_RECOVERY_SESSION")
        if recovery_session and worker_id is None:
            if len(recovery_session) != 32 or any(ch not in "0123456789abcdef" for ch in recovery_session):
                raise ValueError("invalid recovery session")
            self.worker_id = f"{os.getpid()}-{recovery_session}-{self.lane}"
        self.stop_event = threading.Event()
        self._last_heartbeat = 0.0
        # Capture at startup so an old process cannot report newly replaced files.
        import hashlib
        root = Path(__file__).resolve().parent
        self.runtime_fingerprint = hashlib.sha256(b"".join(
            path.name.encode() + path.read_bytes() for path in sorted(root.glob("*.py"))
        )).hexdigest()
        initialize_queue(connection)
        from .friction import start_observing
        start_observing(connection)

    def _bucket(self, seconds: int) -> int:
        return int(time.time() // seconds)

    def schedule_backlog_generation(self) -> int:
        created = 0
        if not hasattr(self, "_last_orphan_recovery") or time.monotonic() - self._last_orphan_recovery > 60:
            from .process_containment import recover_verified_holds, supersede_replay_safe_periodic_holds
            superseded = supersede_replay_safe_periodic_holds(self.connection)
            recovered = recover_verified_holds(self.connection) + recover_orphaned(self.connection)
            converged = converge_periodic_jobs(self.connection)
            if superseded:
                LOG.warning("superseded %s stale replay-safe contained jobs", superseded)
            if recovered:
                LOG.warning("recovered %s expired running jobs during steady state", recovered)
            if converged:
                LOG.warning("cancelled %s obsolete duplicate periodic jobs", converged)
            self._last_orphan_recovery = time.monotonic()
        specs = (
            (
                f"coordinator.scan:{self._bucket(self.coordinator_scan_seconds)}",
                "coordinator.scan",
                95,
            ),
            (
                f"maintainer.scan:{self._bucket(self.maintainer_scan_seconds)}",
                "maintainer.scan",
                100,
            ),
            (
                f"performance.scan:{self._bucket(self.performance_scan_seconds)}",
                "performance.scan",
                99,
            ),
            (
                f"reality.scan:{self._bucket(self.reality_scan_seconds)}",
                "reality.scan",
                98,
            ),
        )
        specs += ((f"revenue.engine:{self._bucket(self.performance_scan_seconds)}", "revenue.engine", 97),)
        specs += ((f"brain.cycle:{self._bucket(self.reality_scan_seconds)}", "brain.cycle", 97),)
        specs += ((f"revenue.followthrough:{self._bucket(self.reality_scan_seconds)}", "revenue.followthrough", 96),)
        specs += ((f"maturity.recovery_drill:{self._bucket(86400)}", "maturity.recovery_drill", 60),)
        for fingerprint, kind, priority in specs:
            _, was_created = enqueue_if_kind_idle(
                self.connection,
                fingerprint=fingerprint,
                kind=kind,
                priority=priority,
            )
            created += int(was_created)
        from .engineering import schedule_waiting
        from .engineering_delivery import schedule_promotions
        created += schedule_waiting(self.connection, allow_probe=self.lane == "all")
        created += schedule_promotions(self.connection)
        from .command_center import schedule_due_jobs
        created += schedule_due_jobs(self.connection)
        if not hasattr(self, "_last_request_recheck") or time.monotonic() - self._last_request_recheck > 300:
            from .request_fabric import recheck_waiting, recheck_sandbox
            recheck_waiting(self.connection)
            recheck_sandbox(self.connection, self.home)
            self._last_request_recheck = time.monotonic()
        if not hasattr(self, "_last_dead_letter_reconcile") or time.monotonic() - self._last_dead_letter_reconcile > 60:
            from .dead_letter import reconcile as reconcile_dead_letters
            reconcile_dead_letters(self.connection, limit=100)
            self._last_dead_letter_reconcile = time.monotonic()
        return created

    def heartbeat(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_heartbeat < 30:
            return
        payload = {
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "timestamp_epoch": now,
            "runtime_fingerprint": self.runtime_fingerprint,
            "runtime_root": str(Path(__file__).resolve().parent),
        }
        active = getattr(self, "_active_job", None)
        if active is not None:
            from datetime import datetime, timedelta, timezone
            expires = (datetime.now(timezone.utc)+timedelta(seconds=self.lease_seconds)).isoformat()
            self.connection.execute("UPDATE worker_jobs SET lease_expires_at=? WHERE id=? AND state='running' AND lease_owner=?", (expires,active.id,self.worker_id))
        set_state(self.connection, f"worker.{self.lane}.heartbeat", json.dumps(payload, sort_keys=True))
        if self.lane != "heavy":
            set_state(self.connection, "worker.heartbeat", json.dumps(payload, sort_keys=True))
            set_state(self.connection, "worker.pid", str(os.getpid()))
        self._last_heartbeat = now

    def _dispatch(self, job: Job) -> dict[str, Any]:
        from . import request_fabric
        if job.kind == "command_center.directive":
            from .command_center import execute_directive_job
            return execute_directive_job(self.connection, job, home=self.home)
        if job.kind == "command_center.scheduled":
            from .command_center import execute_scheduled_job
            return execute_scheduled_job(self.connection, job, home=self.home)
        if job.kind == "engineering.build":
            from .engineering import execute_run
            return execute_run(self.connection, job, home=self.home, pulse=self.heartbeat)
        if job.kind in {"engineering.promote", "engineering.ci_watch", "engineering.ci_repair"}:
            from .engineering_delivery import execute_ci_repair, execute_ci_watch, execute_promotion
            handler = {
                "engineering.promote": execute_promotion,
                "engineering.ci_watch": execute_ci_watch,
                "engineering.ci_repair": execute_ci_repair,
            }[job.kind]
            return handler(self.connection, job, home=self.home, pulse=self.heartbeat)
        if job.kind == "request.execute":
            return request_fabric.execute_request(self.connection, job, home=self.home, pulse=self.heartbeat)
        if job.kind == "capability.build":
            return request_fabric.build_capability(self.connection, job, home=self.home, pulse=self.heartbeat)
        if job.kind == "revenue.engine":
            from .revenue_engine import control_cycle
            return control_cycle(self.connection)
        if job.kind == "brain.cycle":
            from .autonomy_brain import control_cycle
            return control_cycle(self.connection)
        if job.kind == "maintainer.audit":
            return roles.maintainer_audit(self.connection, job, pulse=self.heartbeat)
        handlers: dict[str, Callable[[sqlite3.Connection, Job], dict[str, Any]]] = {
            "coordinator.scan": roles.coordinator_scan,
            "coordinator.plan_snapshot": roles.coordinator_plan_snapshot,
            "coordinator.commitment_sweep": roles.coordinator_commitment_sweep,
            "coordinator.purchase_sweep": roles.coordinator_purchase_sweep,
            "maintainer.scan": roles.maintainer_scan,
            "maintainer.autonomy_health": roles.maintainer_autonomy_health,
            "maintainer.architecture_gap": roles.maintainer_architecture_gap,
            "maintainer.capability_probe": roles.maintainer_capability_probe,
            "maintainer.architecture_gap": roles.maintainer_architecture_gap,
            "maintainer.production_readiness": roles.maintainer_production_readiness,
            "maintainer.communication_evidence": roles.maintainer_communication_evidence,
            "maintainer.queue_health": roles.maintainer_queue_health,
            "maturity.recovery_drill": roles.maturity_recovery_drill,
            "performance.scan": roles.performance_scan,
            "reality.scan": roles.reality_scan,
            "revenue.followthrough": roles.revenue_followthrough_scan,
        }
        if job.kind == "maintainer.backup":
            return roles.maintainer_backup(self.connection, job, self.backups, pulse=self.heartbeat)
        handler = handlers.get(job.kind)
        if handler is None:
            raise ValueError(f"unknown job kind: {job.kind}")
        return handler(self.connection, job)

    def run_one(self) -> bool:
        self.heartbeat()
        if get_state(self.connection, "worker.emergency_stop") == "1":
            return False
        if get_state(self.connection, "worker.paused") == "1":
            return False
        if self.lane != "heavy":
            self.schedule_backlog_generation()
        job = claim_next(
            self.connection,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            lane=self.lane,
        )
        if job is None:
            return False
        LOG.info("claimed job id=%s kind=%s attempt=%s", job.id, job.kind, job.attempts)
        self._active_job = job
        try:
            result = self._dispatch(job)
            if not complete(
                self.connection, job, worker_id=self.worker_id, result=result
            ):
                raise RuntimeError("job lease lost before completion")
            LOG.info("completed job id=%s kind=%s", job.id, job.kind)
        except ProcessContainmentError as exc:
            # Leave the original lease running. The controller owns this entire
            # process tree and must verify exit before recovering its exact job.
            from .events import append_event
            from .process_containment import hold_owner
            hold_owner(self.connection, self.worker_id, pid=os.getpid(),
                       identities=exc.identities, snapshot_complete=exc.snapshot_complete)
            append_event(self.connection, "worker.containment_unconfirmed", {
                "worker_id": self.worker_id, "job_id": job.id, "lane": self.lane,
                "disposition": "fatal_lane_exit_preserve_running_lease",
            })
            self.stop_event.set()
            raise
        except Exception as exc:
            base_delay = min(300, 5 * (2 ** max(0, job.attempts - 1)))
            jitter = random.uniform(0, base_delay * 0.25)
            delay = base_delay + jitter
            state = fail(
                self.connection,
                job,
                worker_id=self.worker_id,
                error=f"{type(exc).__name__}: {exc}",
                retry_delay_seconds=delay,
            )
            if job.kind.startswith("command_center."):
                try:
                    import traceback
                    from .command_center import record_job_failure
                    record_job_failure(
                        self.connection,
                        job,
                        error=exc,
                        state=state,
                        traceback_text=traceback.format_exc(),
                    )
                except Exception:
                    LOG.exception("command-center failure recorder failed job=%s", job.id)
            LOG.exception("job failed id=%s kind=%s state=%s", job.id, job.kind, state)
        finally:
            self._active_job = None
        return True

    def run_until_idle(self, max_jobs: int = 100) -> int:
        count = 0
        while count < max_jobs:
            if not self.run_one():
                break
            count += 1
        self.heartbeat(force=True)
        return count

    def request_stop(self, *_args: object) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        self.heartbeat(force=True)
        LOG.info("worker started id=%s", self.worker_id)
        while not self.stop_event.is_set():
            did_work = self.run_one()
            if did_work:
                continue
            self.stop_event.wait(self.poll_seconds)
        self.heartbeat(force=True)
        LOG.info("worker stopped id=%s", self.worker_id)

def install_signal_handlers(worker: Worker) -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, worker.request_stop)
        except (OSError, ValueError):
            pass

def worker_status(connection: sqlite3.Connection) -> dict[str, Any]:
    initialize_queue(connection)
    heartbeat_raw = get_state(connection, "worker.heartbeat")
    audit_raw = get_state(connection, "maintainer.last_audit")
    backup_raw = get_state(connection, "maintainer.last_backup")
    performance_raw = get_state(connection, "performance.last_scan")
    reality_raw = get_state(connection, "reality.last_scan")
    architecture_raw = get_state(connection, "architecture.last_gap_scan")
    def decode(raw: str | None) -> Any:
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    from .engineering import summary as engineering_summary
    from .engineering_delivery import summary as engineering_delivery_summary
    from .architecture_gap import summary as architecture_gap_summary
    return {
        "heartbeat": decode(heartbeat_raw),
        "audit": decode(audit_raw),
        "backup": decode(backup_raw),
        "performance": decode(performance_raw),
        "reality": decode(reality_raw),
        "architecture": decode(architecture_raw),
        "queue": stats(connection),
        "engineering": engineering_summary(connection),
        "engineering_delivery": engineering_delivery_summary(connection),
        "architecture_gaps": architecture_gap_summary(connection),
    }

def run_worker(
    connection: sqlite3.Connection,
    *,
    home: Path,
    backups: Path,
    log_path: Path,
    once: bool = False,
    poll_seconds: float = 3.0,
) -> int:
    # The controller assigns kernel process ownership before opening this gate.
    gate = os.environ.get("LIFE_OS_WORKER_START_GATE")
    if gate:
        deadline = time.monotonic()+30
        while not Path(gate).is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("controller did not establish worker process ownership")
            time.sleep(.05)
    configure_logging(log_path)
    initialize_queue(connection)
    lane = os.environ.get("LIFE_OS_WORKER_LANE", "all")
    lock_path = home / ("worker.lock" if lane in {"all", "light"} else "worker-heavy.lock")
    with InstanceLock(lock_path):
        recovered = recover_orphaned(connection, force_all_running=lane == "all")
        if recovered:
            LOG.warning("recovered %s interrupted running jobs", recovered)
        worker = Worker(
            connection,
            home=home,
            backups=backups,
            poll_seconds=poll_seconds,
        )
        install_signal_handlers(worker)
        if once:
            worker.run_until_idle()
        else:
            worker.run_forever()
    return 0
