"""Durable schema for the LIFE OS Central Command Hub."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info'
        CHECK(level IN ('debug','info','warning','error','critical')),
    message TEXT NOT NULL,
    task_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_activity_logs_time
ON activity_logs(occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    directive TEXT NOT NULL,
    schedule_kind TEXT NOT NULL
        CHECK(schedule_kind IN ('once','interval_seconds','cron')),
    schedule_value TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','paused','completed','cancelled')),
    risk_level TEXT NOT NULL DEFAULT 'low'
        CHECK(risk_level IN ('low','medium','high','critical')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK(max_retries BETWEEN 1 AND 10),
    last_run_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
ON scheduled_tasks(state, next_run_at);

CREATE TABLE IF NOT EXISTS pending_reminders (
    id TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','fired','dismissed')),
    created_at TEXT NOT NULL,
    fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_reminders_due
ON pending_reminders(state, remind_at);

CREATE TABLE IF NOT EXISTS system_errors (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    occurred_at TEXT NOT NULL,
    severity TEXT NOT NULL
        CHECK(severity IN ('warning','error','critical')),
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    traceback TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open'
        CHECK(state IN ('open','acknowledged','resolved')),
    retryable INTEGER NOT NULL DEFAULT 1 CHECK(retryable IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_system_errors_open
ON system_errors(state, occurred_at DESC);

CREATE TABLE IF NOT EXISTS agent_states (
    agent_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    current_task_id TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""

def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()
