"""Health-domain helpers. Tracking only; not diagnosis."""
from __future__ import annotations
import sqlite3
from datetime import date
from .metrics import record,history

def record_sleep(c:sqlite3.Connection,hours:float,day:date|None=None): return record(c,"sleep","duration",hours,"hours",day)
def record_weight(c:sqlite3.Connection,lb:float,day:date|None=None): return record(c,"fitness","weight",lb,"lb",day)
def record_training(c:sqlite3.Connection,minutes:float,day:date|None=None): return record(c,"fitness","training",minutes,"minutes",day)
def latest_weight(c:sqlite3.Connection):
    rows=history(c,"fitness","weight",1); return rows[0]["value"] if rows else None
