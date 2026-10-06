"""Daily human-state check-ins."""
from __future__ import annotations
import sqlite3
from datetime import date

def save_checkin(c:sqlite3.Connection,day:date|None=None,sleep_hours:float|None=None,energy:int|None=None,mood:int|None=None,pain:int|None=None,exercise_minutes:int=0,note:str="")->None:
    if sleep_hours is not None and not 0 <= sleep_hours <= 24: raise ValueError("sleep must be 0..24")
    for name,value,lo in (("energy",energy,1),("mood",mood,1),("pain",pain,0)):
        if value is not None and not lo <= value <= 10: raise ValueError(f"{name} out of range")
    d=(day or date.today()).isoformat()
    c.execute("""INSERT INTO checkins(occurred_on,sleep_hours,energy,mood,pain,exercise_minutes,note)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(occurred_on) DO UPDATE SET sleep_hours=excluded.sleep_hours,energy=excluded.energy,mood=excluded.mood,pain=excluded.pain,exercise_minutes=excluded.exercise_minutes,note=excluded.note""",(d,sleep_hours,energy,mood,pain,exercise_minutes,note)); c.commit()

def latest(c:sqlite3.Connection):
    return c.execute("SELECT * FROM checkins ORDER BY occurred_on DESC LIMIT 1").fetchone()
