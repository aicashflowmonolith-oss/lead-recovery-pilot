"""Relationship maintenance without storing private message content."""
from __future__ import annotations
import sqlite3
from datetime import date
from .metrics import record
def log_meaningful_contact(c:sqlite3.Connection,count:int=1,day:date|None=None):
    if count<0: raise ValueError("count cannot be negative")
    return record(c,"relationships","meaningful_contact",count,"count",day)
