"""Daily/weekly review summaries."""
from __future__ import annotations
import sqlite3
from datetime import date,timedelta

def summary(c:sqlite3.Connection,days:int=7)->dict:
    if days < 1: raise ValueError("days must be positive")
    since=(date.today()-timedelta(days=days-1)).isoformat()
    completed=c.execute("SELECT COUNT(*) FROM events WHERE kind IN ('task.completed','routine.completed') AND occurred_at>=?",(since,)).fetchone()[0]
    spending=c.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE amount_cents<0 AND occurred_on>=?",(since,)).fetchone()[0]
    checkins=c.execute("SELECT COUNT(*) FROM checkins WHERE occurred_on>=?",(since,)).fetchone()[0]
    return {"days":days,"completed_actions":completed,"spending_cents":abs(spending),"checkins":checkins}
