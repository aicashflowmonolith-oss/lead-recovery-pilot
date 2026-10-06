"""Compact state snapshot."""
from __future__ import annotations
import sqlite3
from datetime import date
from .finance import balances
from .purchases import queue
from .checkins import latest

def snapshot(c:sqlite3.Connection)->dict:
    check=latest(c)
    return {
        "date":date.today().isoformat(),
        "open_tasks":c.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0],
        "active_goals":c.execute("SELECT COUNT(*) FROM goals WHERE status='active'").fetchone()[0],
        "net_cash_cents":sum(x[2] for x in balances(c)),
        "purchase_queue":len(queue(c)),
        "latest_checkin":dict(check) if check else None,
    }
