"""MONOLITH's deterministic local recovery executor. No model or network dependency.

Authority is supplied by an exact-commit activation manifest, never inferred from
model output. Only processes created by this controller may be terminated. LIFE
OS remains the durable ledger; completed jobs and dirty checkouts are preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from .queue import get_state, set_state
from .worker import InstanceLock
from .execution_lanes import HEAVY_JOB_KINDS as HEAVY_KINDS

BOOTSTRAP_VERSION = 3
COUNTERS_KEY = "recovery.counters"
PRESSURE_STOP_PERCENT = 90
PRESSURE_RESUME_PERCENT = 85
PRESSURE_RESUME_DWELL_SECONDS = 60


def git(repo, *args):
    result = subprocess.run(["git", "-c", f"safe.directory={repo}", *args], cwd=repo,
                            capture_output=True, text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError("recovery Git verification failed")
    return result.stdout.strip()


def validate_runtime(entry):
    repo = Path(entry["repo"]).resolve()
    commit = entry["commit"]
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise ValueError("activation requires an exact commit")
    if git(repo, "rev-parse", "HEAD") != commit:
        raise ValueError("activation head changed")
    if git(repo, "status", "--porcelain"):
        raise ValueError("activation checkout is dirty; preserve it")
    worker_source = (repo/"life_os"/"worker.py").read_text(encoding="utf-8")
    if any(marker not in worker_source for marker in (
        "LIFE_OS_WORKER_LANE", "LIFE_OS_WORKER_START_GATE", "LIFE_OS_RECOVERY_SESSION")):
        raise ValueError("runtime lacks lane/session/ownership handshake support")
    controller_source = (repo/"life_os"/"recovery_controller.py").read_text(encoding="utf-8")
    version = re.search(r"^BOOTSTRAP_VERSION\s*=\s*(\d+)", controller_source, re.MULTILINE)
    if not version or int(version.group(1)) < BOOTSTRAP_VERSION:
        raise ValueError("runtime lacks compatible durable containment bootstrap")
    return repo


def qualified_bootstrap_label(manifest, *, source_root=None):
    """Bind the running controller's actual source to an exact approved entry."""
    root = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    for label in ("active", "rollback"):
        entry = manifest[label]
        if Path(entry["repo"]).resolve() == root:
            validate_runtime(entry)
            return label
    raise ValueError("controller source is outside the approved activation")


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("activation manifest must be an object")
    if data.get("authority") != "monolith" or data.get("scope") != "reversible_zero_cost_local_recovery":
        raise ValueError("MONOLITH recovery authority required")
    if not isinstance(data.get("authorization_ref"), str) or not data["authorization_ref"]:
        raise ValueError("authorization provenance required")
    if type(data.get("approval_id")) is not int or data["approval_id"] <= 0:
        raise ValueError("exact activation approval required")
    for key in ("active", "rollback"):
        entry = data.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str) or not entry["repo"]:
            raise ValueError("activation runtime path required")
        commit = entry.get("commit")
        if not isinstance(commit, str) or len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
            raise ValueError("activation requires an exact commit")
    # Qualification happens after canonical authority verification. A dirty or
    # broken active tree must not prevent the approved clean rollback booting.
    return data


def verify_authority(connection, manifest):
    row = connection.execute("SELECT action,state,payload_json,expires_at FROM approvals WHERE id=?",
                             (manifest.get("approval_id"),)).fetchone()
    expected = {"scope": "reversible_zero_cost_local_recovery",
                "active_commit": manifest["active"]["commit"],
                "rollback_commit": manifest["rollback"]["commit"],
                "authorization_ref": manifest["authorization_ref"]}
    if not row or row["action"] != "ACTIVATE_LOCAL_RECOVERY" or row["state"] != "approved":
        raise ValueError("exact local-recovery activation authority unavailable")
    if row["expires_at"]:
        from datetime import datetime, timezone
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("local-recovery activation authority expired")
    payload = json.loads(row["payload_json"])
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("local-recovery authority does not match activation")
    if payload.get("cost_cents") != 0 or payload.get("production_deployment") is not False:
        raise ValueError("local recovery cannot grant spending or production authority")


def recover_owned_jobs(connection, pid, *, worker_id=None, verified_exit=False, expected_hold=None):
    """Called only after a controller-owned process tree has exited.

    Reuse the original job and payload, preserve attempt accounting and receipts.
    A kill is infrastructure interruption, not a new engineering objective.
    """
    from .process_containment import read_holds
    holds = read_holds(connection)
    if not verified_exit and (worker_id in holds or (worker_id is None and any(
            item.get("pid") == pid for item in holds.values()))):
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "+00:00"
    connection.execute("BEGIN IMMEDIATE")
    try:
        if expected_hold is not None and read_holds(connection).get(worker_id) != expected_hold:
            connection.rollback()
            return None
        owner_sql = "lease_owner=?" if worker_id is not None else "lease_owner LIKE ?"
        owner = worker_id if worker_id is not None else str(pid)+"-%"
        rows = connection.execute("SELECT id FROM worker_jobs WHERE state='running' AND "+owner_sql,
                                  (owner,)).fetchall()
        for row in rows:
            connection.execute("UPDATE worker_jobs SET state='retry',available_at=?,updated_at=?,"
                               "lease_owner=NULL,lease_expires_at=NULL,max_attempts=max(max_attempts,attempts+1),"
                               "last_error='controller-owned worker interrupted; resume original job' WHERE id=? AND state='running'",
                               (now, now, row[0]))
            connection.execute("INSERT INTO worker_job_events(job_id,kind,occurred_at,payload_json) VALUES(?,?,?,?)",
                               (row[0], "recovery.resumed", now, json.dumps({"pid": pid})))
        if expected_hold is not None:
            from .process_containment import HOLDS_KEY
            holds = read_holds(connection)
            holds.pop(worker_id)
            set_state(connection, HOLDS_KEY, json.dumps(holds, sort_keys=True))
            from .events import append_event
            append_event(connection, "recovery.containment_verified", expected_hold)
        else:
            connection.commit()
        return len(rows)
    except Exception:
        connection.rollback()
        raise


def recover_previous_session(connection):
    """Only use exact durable owners from Windows kernel-contained workers.

    Call after obtaining the controller, light, and heavy singleton locks. This
    never terminates a process named by persisted PID; it resumes ledger work.
    """
    data = json.loads(get_state(connection, "recovery.owned_workers") or "{}")
    if os.name != "nt" or data.get("containment") != "windows_job_kill_on_close":
        return 0
    owners = data.get("workers", {})
    from .process_containment import read_holds, exact_exit_verified, clear_hold
    holds = read_holds(connection)
    retained = {}
    recovered = 0
    for lane, item in owners.items():
        worker_id = item.get("worker_id", "")
        session = item.get("session", "")
        pid = item.get("pid")
        if lane not in {"light", "heavy"} or type(pid) is not int:
            raise ValueError("invalid previous recovery ownership")
        if len(session) != 32 or any(ch not in "0123456789abcdef" for ch in session):
            raise ValueError("invalid previous recovery session")
        if worker_id != f"{pid}-{session}-{lane}":
            raise ValueError("previous recovery owner does not match session")
        if worker_id in holds:
            if not exact_exit_verified(holds[worker_id]):
                retained[lane] = item
                continue
            resumed = recover_owned_jobs(connection, pid, worker_id=worker_id, verified_exit=True,
                                         expected_hold=holds[worker_id])
            if resumed is None:
                retained[lane] = item
                continue
            recovered += resumed
        else:
            recovered += recover_owned_jobs(connection, pid, worker_id=worker_id)
    set_state(connection, "recovery.owned_workers", json.dumps({"containment": "windows_job_kill_on_close", "workers": retained}))
    return recovered


def metrics(connection, window_seconds=3600):
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time()-window_seconds)) + "+00:00"
    rows = connection.execute("SELECT kind,state,count(*) n FROM worker_jobs GROUP BY kind,state").fetchall()
    oldest = connection.execute("SELECT min(available_at) FROM worker_jobs WHERE state IN ('queued','retry')").fetchone()[0]
    latency = connection.execute("SELECT avg((julianday(e.occurred_at)-julianday(COALESCE(json_extract(e.payload_json,'$.ready_at'),j.available_at)))*86400) "
                                 "FROM worker_job_events e JOIN worker_jobs j ON j.id=e.job_id "
                                 "WHERE e.kind='claimed' AND e.occurred_at>=?", (cutoff,)).fetchone()[0]
    # Verified commits, not completed scheduler jobs, count as engineering output.
    verified = connection.execute("SELECT count(DISTINCT commit_sha) FROM engineering_runs WHERE state='verified' "
                                  "AND commit_sha!='' AND finished_at>=?", (cutoff,)).fetchone()[0]
    oldest_age = connection.execute("SELECT (julianday('now')-julianday(min(available_at)))*86400 FROM worker_jobs WHERE state IN ('queued','retry')").fetchone()[0]
    return {"queue": [dict(r) for r in rows], "oldest_ready_at": oldest,
            "oldest_ready_age_seconds": max(0, oldest_age) if oldest_age is not None else None,
            "ready_to_start_seconds_mean": latency,
            "verified_engineering_completions_per_hour": verified*3600/window_seconds}


class Controller:
    def __init__(self, connection, manifest, *, home, db, clock=time.monotonic, bootstrap_label=None):
        self.connection = connection
        self.manifest = manifest
        self.home, self.db = Path(home).resolve(), Path(db).resolve()
        self.clock = clock
        if bootstrap_label not in {None, "active", "rollback"}:
            raise ValueError("invalid qualified bootstrap selection")
        self.bootstrap_label = bootstrap_label
        self.bootstrap_forced_rollback = False
        self.children = {}
        self.last_probe = 0
        self.started = clock()
        self.useful_seconds = 0.0
        self.last_tick = self.started
        self.selected = "active"
        self.failures = {"light": 0, "heavy": 0}
        self.next_start = {"light": 0.0, "heavy": 0.0}
        self.recoveries = 0
        self.logs = {}
        self.containers = {}
        self.gates = {}
        self.session = uuid.uuid4().hex
        self.failure_started = {}
        self.last_recovery_seconds = None
        self.pressure_clear_since = None
        self.pressure_resume_started = False
        self.activation_identity = {
            key: manifest.get(key) for key in ("authority", "scope", "authorization_ref", "approval_id")
        }
        self.activation_identity.update({key: {
            "repo": str(Path(manifest[key]["repo"]).resolve()), "commit": manifest[key]["commit"]
        } for key in ("active", "rollback")})
        self.restore_counters()

    def restore_counters(self):
        raw = get_state(self.connection, COUNTERS_KEY)
        try:
            saved = json.loads(raw or "{}")
            if (not isinstance(saved, dict) or (raw and saved.get("version") != 1)
                    or any(not isinstance(saved.get(key, {}), dict) for key in ("recurrence", "activation_recurrence", "resource"))):
                raise ValueError("invalid recovery counters")
        except ValueError:
            saved = {}
            self.counter_restore_state = "invalid_previous_state"
        else:
            self.counter_restore_state = "restored" if raw else "new_observation_interval"

        def count(value):
            return value if type(value) is int and value >= 0 else 0

        self.counter_since_epoch = saved.get("since_epoch", time.time())
        self.recoveries = count(saved.get("recoveries"))
        self.failures = {lane: count(saved.get("recurrence", {}).get(lane)) for lane in ("light", "heavy")}
        same_activation = saved.get("activation") == self.activation_identity
        self.activation_failures = {lane: count(saved.get("activation_recurrence", {}).get(lane)) if same_activation else 0
                                    for lane in ("light", "heavy")}
        self.selected = saved.get("selected", "active") if same_activation else "active"
        if self.selected not in {"active", "rollback"}:
            self.selected = "active"
        self.bootstrap_forced_rollback = self.bootstrap_label == "rollback" and self.selected != "rollback"
        if self.bootstrap_label == "rollback":
            self.selected = "rollback"
        self.resource_counters = {key: count(saved.get("resource", {}).get(key)) for key in (
            "pressure_episodes", "containments", "resume_starts", "verified_resumes")}
        self.previous_session_resumed_jobs = count(saved.get("previous_session_resumed_jobs"))
        self.last_recovery_seconds = saved.get("last_verified_recovery_seconds")
        self.pressure_resume_pending = saved.get("pressure_resume_pending") is True
        self.pressure_suspended = (saved.get("pressure_suspended") is True or self.pressure_resume_pending
                                   or self.counter_restore_state == "invalid_previous_state")
        # A monotonic clear dwell cannot be carried across a process/reboot.
        self.pressure_clear_since = None
        self.pressure_resume_started = False
        self._last_saved_counters = raw

    def persist_counters(self):
        value = json.dumps({"version": 1, "since_epoch": self.counter_since_epoch,
                            "activation": self.activation_identity, "selected": self.selected,
                            "recurrence": self.failures, "activation_recurrence": self.activation_failures,
                            "recoveries": self.recoveries, "resource": self.resource_counters,
                            "previous_session_resumed_jobs": self.previous_session_resumed_jobs,
                            "last_verified_recovery_seconds": self.last_recovery_seconds,
                            "pressure_suspended": self.pressure_suspended,
                            "pressure_resume_pending": self.pressure_resume_pending}, sort_keys=True)
        if value != self._last_saved_counters:
            set_state(self.connection, COUNTERS_KEY, value)
            self._last_saved_counters = value

    def select_rollback(self, reason):
        validate_runtime(self.manifest["rollback"])
        self.selected = "rollback"
        self.persist_counters()
        self.record("rolled_back", commit=self.manifest["rollback"]["commit"], reason=reason)

    def qualify_runtime(self):
        if "approval_id" in self.manifest:
            verify_authority(self.connection, self.manifest)
        if self.bootstrap_forced_rollback:
            validate_runtime(self.manifest["rollback"])
            self.persist_counters()
            self.record("rolled_back", commit=self.manifest["rollback"]["commit"],
                        reason="qualified_rollback_bootstrap_loaded")
            self.bootstrap_forced_rollback = False
        try:
            return validate_runtime(self.manifest[self.selected])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if self.selected != "active":
                raise
            self.record("activation_rejected", lane="bootstrap", error_type=type(exc).__name__)
            self.select_rollback("active_bootstrap_validation_failed")
            return Path(self.manifest["rollback"]["repo"]).resolve()

    def update_pressure(self, now, pressure, load_percent):
        if pressure or (load_percent is not None and load_percent >= PRESSURE_STOP_PERCENT):
            self.pressure_clear_since = None
            if not self.pressure_suspended:
                self.pressure_suspended = True
                self.pressure_resume_pending = False
                self.pressure_resume_started = False
                self.resource_counters["pressure_episodes"] += 1
                self.persist_counters()
                self.record("memory_pressure_entered", load_percent=load_percent,
                            stop_percent=PRESSURE_STOP_PERCENT, resume_percent=PRESSURE_RESUME_PERCENT,
                            resume_dwell_seconds=PRESSURE_RESUME_DWELL_SECONDS)
        elif self.pressure_suspended:
            if load_percent is not None and load_percent > PRESSURE_RESUME_PERCENT:
                self.pressure_clear_since = None
            else:
                if self.pressure_clear_since is None:
                    self.pressure_clear_since = now
                # At a float binade boundary, subtracting two monotonic times
                # can round an exact deadline's dwell below 60 seconds. Compare
                # the absolute deadline; never admit an earlier clock sample.
                if now >= self.pressure_clear_since + PRESSURE_RESUME_DWELL_SECONDS:
                    self.pressure_suspended = False
                    self.pressure_resume_pending = True
                    self.pressure_resume_started = False
                    self.persist_counters()
                    self.record("memory_pressure_cleared", load_percent=load_percent,
                                stable_dwell_seconds=now-self.pressure_clear_since)

    def record(self, kind, **payload):
        from .events import append_event
        append_event(self.connection, "recovery."+kind, payload)

    def start(self, lane):
        repo = validate_runtime(self.manifest[self.selected])
        env = os.environ.copy()
        env["LIFE_OS_WORKER_LANE"] = lane
        env["LIFE_OS_RECOVERY_SESSION"] = self.session
        container = None
        gate = None
        if os.name == "nt":
            from .owned_processes import WindowsJob
            container = WindowsJob()
            gate_dir = self.home/"runtime"/"recovery-gates"
            gate_dir.mkdir(parents=True, exist_ok=True)
            gate = gate_dir/(uuid.uuid4().hex+".ready")
            env["LIFE_OS_WORKER_START_GATE"] = str(gate)
        log_dir = self.home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / ("recovery-"+lane+".log"), "ab")
        try:
            child = subprocess.Popen([sys.executable, "-m", "life_os", "--db", str(self.db), "worker",
                                      "--home", str(self.home), "--backups", str(self.home/"backups"),
                                      "--log", str(log_dir/("worker-"+lane+".log"))], cwd=repo, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=os.name != "nt",
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            log.close()
            if container:
                container.close()
            raise
        try:
            if container:
                container.assign(child)
                owners = json.loads(get_state(self.connection, "recovery.owned_workers") or "{}")
                workers = owners.get("workers", {})
                workers[lane] = {"pid": child.pid, "session": self.session,
                                 "worker_id": f"{child.pid}-{self.session}-{lane}"}
                set_state(self.connection, "recovery.owned_workers", json.dumps({
                    "containment": "windows_job_kill_on_close", "workers": workers}))
                gate.touch(exist_ok=False)
        except Exception:
            child.kill()
            child.wait(timeout=10)
            log.close()
            if container:
                container.close()
            raise
        self.children[lane] = (child, self.clock())
        self.logs[lane] = log
        if container:
            self.containers[lane] = container
            self.gates[lane] = gate
        self.record("worker_started", lane=lane, pid=child.pid, commit=self.manifest[self.selected]["commit"])

    def stop(self, lane):
        pair = self.children.get(lane)
        if pair is None:
            return 0
        child = pair[0]
        container = self.containers.get(lane)
        if container:
            # Descendants remain in the kernel job after their worker exits.
            # Contain and verify the entire tree before any job is requeued.
            from .process_containment import hold_owner, read_holds
            worker_id = f"{child.pid}-{self.session}-{lane}"
            prior = read_holds(self.connection).get(worker_id)
            prior_complete = prior is None or prior.get("snapshot_complete") is True
            root_handle = getattr(child, "_handle", None)
            root_identity = container.process_identities({child.pid: root_handle}) if isinstance(root_handle, int) else []
            hold_owner(self.connection, worker_id, pid=child.pid, identities=root_identity)
            def persist_snapshot(identities, complete):
                hold_owner(self.connection, worker_id, pid=child.pid, identities=identities,
                           snapshot_complete=bool(complete and prior_complete))
            container.terminate(on_snapshot=persist_snapshot)
            child.wait(timeout=10)
            container.close()
            self.containers.pop(lane)
            self.gates.pop(lane).unlink(missing_ok=True)
        elif child.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], timeout=10,
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        self.children.pop(lane)
        # Never requeue while a process may still be executing that job.
        worker_id = f"{child.pid}-{self.session}-{lane}" if container else None
        history_verified = True
        held = None
        if container:
            from .process_containment import read_holds, exact_exit_verified
            held = read_holds(self.connection).get(worker_id)
            history_verified = bool(held and held.get("snapshot_complete") is True and
                                    exact_exit_verified(held))
        recovered = recover_owned_jobs(self.connection, child.pid, worker_id=worker_id,
                                       verified_exit=True, expected_hold=held) if history_verified else 0
        recovered = recovered or 0
        if container:
            owners = json.loads(get_state(self.connection, "recovery.owned_workers") or "{}")
            owners.get("workers", {}).pop(lane, None)
            set_state(self.connection, "recovery.owned_workers", json.dumps(owners))
        self.logs.pop(lane).close()
        self.record("worker_stopped", lane=lane, resumed=recovered)
        return recovered

    def heartbeat(self, lane, pid):
        raw = get_state(self.connection, f"worker.{lane}.heartbeat")
        try:
            value = json.loads(raw or "{}")
            owner = f"{pid}-{self.session}-{lane}"
            return value if value.get("pid") == pid and value.get("worker_id") == owner else {}
        except ValueError:
            return {}

    def tick(self, *, memory_pressure=False, memory_load_percent=None):
        now = self.clock()
        if "approval_id" in self.manifest:
            try:
                verify_authority(self.connection, self.manifest)
            except ValueError:
                self.stop("light")
                self.stop("heavy")
                status = {"state": "waiting_authority", "timestamp_epoch": time.time()}
                set_state(self.connection, "recovery.status", json.dumps(status))
                return status
        paused = any(get_state(self.connection, key) == "1" for key in
                     ("worker.paused", "worker.emergency_stop", "safe_mode.paused"))
        if not paused:
            from .process_containment import recover_verified_holds
            resumed = recover_verified_holds(self.connection)
            if resumed:
                self.previous_session_resumed_jobs += resumed
                self.record("containment_verified_resumed", jobs=resumed)
        memory_pressure = memory_pressure or (memory_load_percent is not None and memory_load_percent >= PRESSURE_STOP_PERCENT)
        self.update_pressure(now, memory_pressure, memory_load_percent)
        healthy_light = False
        for lane in ("light", "heavy"):
            pair = self.children.get(lane)
            if paused:
                self.stop(lane)
                continue
            if self.pressure_suspended and lane == "heavy":
                resumed = self.stop(lane)
                if pair:
                    self.resource_counters["containments"] += 1
                    self.persist_counters()
                    self.record("memory_pressure_contained", lane=lane, resumed_jobs=resumed)
                continue
            if pair:
                child, started = pair
                beat = self.heartbeat(lane, child.pid)
                fresh = time.time() - beat.get("timestamp_epoch", 0) < 120
                if child.poll() is not None or (now-started > 150 and not fresh):
                    self.stop(lane)
                    self.failures[lane] += 1
                    self.activation_failures[lane] += 1
                    self.failure_started.setdefault(lane, now)
                    self.recoveries += 1
                    self.next_start[lane] = now + min(60, 2**min(self.failures[lane], 6))
                    self.persist_counters()
                    self.record("failure_detected", lane=lane, recurrence=self.failures[lane])
                    if lane == "light" and self.activation_failures[lane] >= 3 and self.selected == "active":
                        self.stop("heavy")
                        self.select_rollback("light_worker_failure_threshold")
                elif fresh:
                    healthy_light |= lane == "light"
                    if lane in self.failure_started:
                        self.last_recovery_seconds = now-self.failure_started.pop(lane)
                        self.persist_counters()
                        self.record("verified_resumed", lane=lane, recovery_seconds=self.last_recovery_seconds)
                    if lane == "heavy" and self.pressure_resume_pending and self.pressure_resume_started:
                        self.pressure_resume_pending = False
                        self.resource_counters["verified_resumes"] += 1
                        self.persist_counters()
                        self.record("memory_pressure_verified_resumed", lane=lane,
                                    verification="fresh_owned_worker_heartbeat")
            if lane not in self.children and now >= self.next_start[lane]:
                try:
                    self.start(lane)
                    if lane == "heavy" and self.pressure_resume_pending:
                        self.pressure_resume_started = True
                        self.resource_counters["resume_starts"] += 1
                        self.persist_counters()
                        self.record("memory_pressure_resume_started", lane=lane)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.failures[lane] += 1
                    self.activation_failures[lane] += 1
                    self.next_start[lane] = now + 60
                    self.persist_counters()
                    self.record("activation_rejected", lane=lane, error_type=type(exc).__name__)
                    if self.selected == "active":
                        self.stop("light")
                        self.stop("heavy")
                        self.select_rollback("worker_activation_failed")
        if healthy_light:
            self.useful_seconds += now - self.last_tick
        self.last_tick = now
        # Deterministic auth/executable probes run in recovery, never in a model.
        # They do not qualify inference success or grant spending authority.
        if not paused and not self.pressure_suspended and now-self.last_probe >= 300:
            from .engineering_providers import probe
            try:
                probe(self.connection, Path(self.manifest[self.selected]["repo"]))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                self.record("probe_failed", error_type=type(exc).__name__)
            self.last_probe = now
        status = {"selected": self.selected, "recoveries": self.recoveries,
                  "controller_runtime_root": str(Path(__file__).resolve().parents[1]),
                  "controller_bootstrap_label": self.bootstrap_label,
                  "controller_bootstrap_version": BOOTSTRAP_VERSION,
                  "last_verified_recovery_seconds": self.last_recovery_seconds,
                  "recurrence": self.failures, "memory_pressure": memory_pressure,
                  "memory_load_percent": memory_load_percent,
                  "heavy_pressure_suspended": self.pressure_suspended,
                  "pressure_clear_dwell_seconds": 0 if self.pressure_clear_since is None else now-self.pressure_clear_since,
                  "resource_recovery": self.resource_counters,
                  "counter_since_epoch": self.counter_since_epoch,
                  "counter_restore_state": self.counter_restore_state,
                  "counter_scope": "persisted_controller_observations_since_counter_since_epoch",
                  "previous_session_resumed_jobs": self.previous_session_resumed_jobs,
                  "useful_work_uptime_ratio": self.useful_seconds/max(.001, now-self.started),
                  "useful_work_uptime_scope": "current_controller_session",
                  "measurement": "fraction of controller observation time with fresh light-worker heartbeat",
                  "timestamp_epoch": time.time()}
        self.persist_counters()
        set_state(self.connection, "recovery.status", json.dumps(status))
        return status

    def close(self):
        for lane in list(self.children):
            self.stop(lane)


def memory_load_percent():
    if os.name != "nt":
        return None
    import ctypes
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (key, ctypes.c_ulonglong) for key in ("total_phys", "avail_phys", "total_page", "avail_page",
                                                "total_virtual", "avail_virtual", "extended")]
    value = Memory()
    value.length = ctypes.sizeof(value)
    return int(value.load) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)) else None


def memory_pressure():
    load = memory_load_percent()
    return (load is None and os.name == "nt") or (load is not None and load >= PRESSURE_STOP_PERCENT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    from .db import connect
    c = connect(args.db)
    # Bootstrap never initializes/migrates the canonical database implicitly.
    c.execute("SELECT 1 FROM worker_state LIMIT 1")
    verify_authority(c, manifest)
    label = qualified_bootstrap_label(manifest)
    controller = Controller(c, manifest, home=args.home, db=args.db, bootstrap_label=label)
    stop = False
    def request_stop(*_):
        nonlocal stop
        stop = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    try:
        with InstanceLock(Path(args.home)/"recovery.lock"):
            controller.restore_counters()
            controller.qualify_runtime()
            # The shared light lock prevents coexistence with the legacy worker.
            # No arbitrary old process is killed to take ownership.
            with InstanceLock(Path(args.home)/"worker.lock"):
                with InstanceLock(Path(args.home)/"worker-heavy.lock"):
                    recovered = recover_previous_session(c)
                    if recovered:
                        controller.previous_session_resumed_jobs += recovered
                        controller.persist_counters()
                        controller.record("previous_session_resumed", jobs=recovered)
            while not stop:
                load = memory_load_percent()
                pressure = (load is None and os.name == "nt") or (load is not None and load >= PRESSURE_STOP_PERCENT)
                controller.tick(memory_pressure=pressure, memory_load_percent=load)
                if args.once:
                    break
                time.sleep(5)
    finally:
        controller.close()
        c.close()


if __name__ == "__main__":
    main()
