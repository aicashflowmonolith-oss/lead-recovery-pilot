"""Blueprint entry points backed by the existing MONOLITH execution fabric.

No model-specific client, second broker, new schema, or fabricated telemetry.
The bridge records requests; the existing worker owns planning and execution.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

from . import request_fabric

REQUIRED_TABLES = {'worker_jobs', 'worker_state', 'execution_requests', 'events', 'owner_attention'}


def connect_existing(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Fail instead of silently creating a competing database or running migrations."""
    path = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES <= tables:
        connection.close()
        raise ValueError('Use the existing initialized MONOLITH/LIFE OS database')
    return connection


def load_rules(path: str | Path) -> dict:
    rules = json.loads(Path(path).read_text(encoding='utf-8'))
    limits = {'max_request_chars': (1, 1000), 'heartbeat_stale_after_seconds': (30, 3600)}
    for name, (low, high) in limits.items():
        if type(rules.get(name)) is not int or not low <= rules[name] <= high:
            raise ValueError(f'Invalid {name}')
    if rules.get('control_plane') != 'MONOLITH' or rules.get('queue') != 'existing_worker_jobs':
        raise ValueError('The blueprint must reuse MONOLITH ownership and queue')
    return rules


class EliteSystemBrain:
    """Submit bounded requests; never interpret model text as execution authority."""
    def __init__(self, connection: sqlite3.Connection, *, max_request_chars: int = 1000):
        if type(max_request_chars) is not int or not 1 <= max_request_chars <= 1000:
            raise ValueError('Invalid request limit')
        self.connection = connection
        self.max_request_chars = max_request_chars

    def submit(self, text: str, *, idempotency_key: str) -> dict:
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= self.max_request_chars:
            raise ValueError('Request is empty or exceeds the configured bound')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 200:
            raise ValueError('A bounded stable idempotency key is required')
        rid = hashlib.sha256(('monolith-core:' + idempotency_key.strip()).encode()).hexdigest()[:32]
        request_fabric.submit(self.connection, text, request_id=rid)
        return self.status(rid)

    def status(self, request_id: str) -> dict:
        row = self.connection.execute(
            'SELECT id,state,provider,generation FROM execution_requests WHERE id=?', (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError('Request does not exist')
        # A completed queue handler can still leave a request waiting for a provider.
        result = dict(row)
        result['execution_owner'] = 'monolith_agent'
        return result


class SystemSentinel:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def snapshot(self, *, now: float | None = None, stale_after_seconds: int = 90) -> dict:
        if type(stale_after_seconds) is not int or not 30 <= stale_after_seconds <= 3600:
            raise ValueError('Invalid heartbeat freshness bound')
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('Invalid observation time')
        states = dict(self.connection.execute(
            "SELECT key,value FROM worker_state WHERE key IN ('worker.heartbeat','worker.paused','worker.emergency_stop')"
        ))
        age = None
        try:
            stamp = json.loads(states.get('worker.heartbeat', '{}')).get('timestamp_epoch')
            if type(stamp) in (int, float) and math.isfinite(stamp) and stamp <= now:
                age = now - stamp
        except (ValueError, AttributeError, TypeError):
            pass
        fresh = age is not None and age <= stale_after_seconds
        stopped = states.get('worker.emergency_stop') == '1'
        paused = states.get('worker.paused') == '1'
        jobs = {r[0]: r[1] for r in self.connection.execute('SELECT state,COUNT(*) FROM worker_jobs GROUP BY state')}
        return {'worker_heartbeat_fresh': fresh, 'heartbeat_age_seconds': age,
                'emergency_stop': stopped, 'paused': paused, 'jobs': jobs,
                'dispatch_state': 'stopped' if stopped else 'paused' if paused else 'available' if fresh else 'unknown',
                'hourly_spend_cents': None, 'spend_evidence': 'not_connected',
                'engineering_or_revenue_success_inferred': False}

    def trip(self, reason: str) -> dict:
        """Durable cooperative stop; not a claim to kill hardware or in-flight APIs."""
        if reason not in {'operator_stop', 'verified_budget_breach', 'verified_resource_breach'}:
            raise ValueError('Use a supported stop reason; do not store arbitrary secret-bearing logs')
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            previous = self.connection.execute("SELECT value,updated_at FROM worker_state WHERE key='worker.emergency_stop'").fetchone()
            changed = previous is None or previous[0] != '1'
            if changed:
                self.connection.execute("INSERT INTO worker_state(key,value,updated_at) VALUES('worker.emergency_stop','1',?) "
                                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (now,))
            episode = now if changed else previous[1]
        # Safety state survives an unavailable attention/audit sink. A subsequent
        # call retries the same episode's receipt without clearing the stop.
        fingerprint = hashlib.sha256(episode.encode()).hexdigest()
        with self.connection:
            attention = self.connection.execute('INSERT OR IGNORE INTO owner_attention(fingerprint,kind,severity,source,payload_json,created_at) VALUES(?,?,?,?,?,?)',
                (f'engine-bridge-stop:{fingerprint}', 'failure_unrepaired', 'error', 'engine_bridge',
                 json.dumps({'reason': reason, 'new_work_stopped': True, 'inflight_cancelled': False}), now))
            if attention.rowcount:
                self.connection.execute('INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)',
                    ('engine_bridge.emergency_stop', now, json.dumps({'reason': reason, 'scope': 'cooperative_worker_admission'})))
        return {'emergency_stop': True, 'changed': changed, 'inflight_cancelled': False, 'external_alert_sent': False}


def assess_budget(*, observed_cents: int | None, authorized_limit_cents: int | None,
                  observation_fresh: bool) -> str:
    """Meter-adapter helper only; does not invent a budget or authorize spending."""
    for value in (observed_cents, authorized_limit_cents):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Money requires nonnegative whole cents or explicit unknown')
    if type(observation_fresh) is not bool:
        raise ValueError('Freshness must be explicit')
    if observed_cents is None or authorized_limit_cents is None or not observation_fresh:
        return 'unknown'
    return 'breached' if observed_cents > authorized_limit_cents else 'within_observed_limit'
