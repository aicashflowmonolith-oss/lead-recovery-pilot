"""Learning tracking."""
from __future__ import annotations
import sqlite3
from datetime import date
from .metrics import record
def log_session(c:sqlite3.Connection,minutes:int,day:date|None=None):
    if minutes<1: raise ValueError("minutes must be positive")
    return record(c,"learning","focused_learning",minutes,"minutes",day)
