"""Generic longitudinal measurements for health, fitness, sleep and other domains."""
from __future__ import annotations
import sqlite3
from datetime import date

def record(c:sqlite3.Connection,domain:str,name:str,value:float,unit:str="",day:date|None=None)->int:
    if not domain.strip() or not name.strip(): raise ValueError("domain and name required")
    cur=c.execute("INSERT INTO metrics(domain,name,value,unit,occurred_on) VALUES(?,?,?,?,?)",(domain.strip(),name.strip(),float(value),unit,(day or date.today()).isoformat())); c.commit(); return int(cur.lastrowid)

def history(c:sqlite3.Connection,domain:str,name:str,limit:int=30)->list[sqlite3.Row]:
    return c.execute("SELECT * FROM metrics WHERE domain=? AND name=? ORDER BY occurred_on DESC,id DESC LIMIT ?",(domain,name,limit)).fetchall()
