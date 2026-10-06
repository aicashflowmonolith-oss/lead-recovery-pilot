"""SQLite persistence for LIFE OS."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from .opportunity import initialize as initialize_opportunity
from .revenue_engine import initialize as initialize_revenue_engine
from .commercial_mailbox import initialize as initialize_commercial_mailbox
from .autonomy_brain import initialize as initialize_brain
from .autonomy_schema import initialize_autonomy
from .foundation import initialize_foundation
from .life_strategy import initialize as initialize_life_strategy
from .known_state import initialize_known_state
from .reality import initialize_reality
from .engineering import initialize as initialize_engineering
from .engineering_delivery import initialize as initialize_engineering_delivery
from .architecture_gap import initialize as initialize_architecture_gap
from .command_center_schema import initialize as initialize_command_center
from .purchases import initialize as initialize_purchases
from .recovery_budget import initialize as initialize_recovery_budget
from .principal_agent import initialize as initialize_principal

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    effort_minutes INTEGER NOT NULL CHECK(effort_minutes > 0),
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    goal_id INTEGER REFERENCES goals(id),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);

CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    effort_minutes INTEGER NOT NULL CHECK(effort_minutes > 0),
    weekdays TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    balance_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL,
    category TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(occurred_on);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    necessity INTEGER NOT NULL DEFAULT 0 CHECK(necessity IN (0,1)),
    status TEXT NOT NULL DEFAULT 'wanted',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    occurred_on TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics(domain,name,occurred_on);

CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    domain TEXT NOT NULL DEFAULT 'life',
    status TEXT NOT NULL DEFAULT 'scheduled'
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    recurring_cost_cents INTEGER NOT NULL DEFAULT 0,
    failure_mode TEXT NOT NULL DEFAULT '',
    recovery_method TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_on TEXT NOT NULL UNIQUE,
    sleep_hours REAL,
    energy INTEGER CHECK(energy BETWEEN 1 AND 10),
    mood INTEGER CHECK(mood BETWEEN 1 AND 10),
    pain INTEGER CHECK(pain BETWEEN 0 AND 10),
    exercise_minutes INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
"""

def connect(path: str | Path) -> sqlite3.Connection:
    db_path=Path(path); db_path.parent.mkdir(parents=True,exist_ok=True)
    connection=sqlite3.connect(db_path, timeout=5.0); connection.row_factory=sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection

def initialize(connection:sqlite3.Connection)->None:
    connection.executescript(SCHEMA)
    initialize_purchases(connection)
    from .request_fabric import initialize as initialize_requests
    initialize_requests(connection)
    initialize_engineering(connection)
    initialize_engineering_delivery(connection)
    initialize_recovery_budget(connection)
    initialize_foundation(connection)
    initialize_life_strategy(connection)
    initialize_known_state(connection)
    initialize_reality(connection)
    initialize_autonomy(connection)
    initialize_architecture_gap(connection)
    initialize_command_center(connection)
    initialize_principal(connection)
    from .money import initialize_collection
    initialize_collection(connection)
    initialize_opportunity(connection)
    initialize_revenue_engine(connection)
    initialize_commercial_mailbox(connection)
    initialize_brain(connection)
    connection.commit()