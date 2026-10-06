"""Recurring routines."""
from __future__ import annotations
import sqlite3
from datetime import date
from .models import Task

def add_routine(c:sqlite3.Connection,title:str,weekdays:set[int],priority:int=50,effort_minutes:int=30)->int:
    if not title.strip(): raise ValueError("routine title must not be empty")
    if not weekdays or any(d not in range(7) for d in weekdays): raise ValueError("weekdays must be 0..6")
    if not 0 <= priority <= 100 or effort_minutes < 1: raise ValueError("invalid priority or effort")
    encoded=",".join(map(str,sorted(weekdays)))
    cur=c.execute("INSERT INTO routines(title,priority,effort_minutes,weekdays) VALUES(?,?,?,?)",(title.strip(),priority,effort_minutes,encoded)); c.commit()
    return int(cur.lastrowid)

def tasks_for_day(c:sqlite3.Connection,day:date)->list[Task]:
    completed={r["routine_id"] for r in c.execute(
        "SELECT json_extract(payload_json,'$.routine_id') AS routine_id FROM events WHERE kind='routine.completed' AND json_extract(payload_json,'$.date')=?",
        (day.isoformat(),)
    )}
    out=[]
    for r in c.execute("SELECT * FROM routines WHERE active=1"):
        if r["id"] not in completed and day.weekday() in {int(x) for x in r["weekdays"].split(",")}:
            out.append(Task(-r["id"],r["title"],r["priority"],r["effort_minutes"],day,"open",None))
    return out
