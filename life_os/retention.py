"""Explicit retention utilities. Destructive operations require direct calls."""
from __future__ import annotations
import sqlite3
from datetime import date,timedelta

def prune_events_before(c:sqlite3.Connection,days:int)->int:
    if days < 30: raise ValueError("minimum retention is 30 days")
    cutoff=(date.today()-timedelta(days=days)).isoformat()
    cur=c.execute("DELETE FROM events WHERE occurred_at < ?",(cutoff,)); c.commit(); return cur.rowcount
