"""Recreation tracking."""
from __future__ import annotations
import sqlite3
from datetime import date
from .metrics import record
def log(c:sqlite3.Connection,minutes:int,day:date|None=None):
    if minutes<0: raise ValueError("minutes cannot be negative")
    return record(c,"recreation","recreation",minutes,"minutes",day)
