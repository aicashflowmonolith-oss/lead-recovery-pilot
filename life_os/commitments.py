"""Time commitments used by the planner."""
from __future__ import annotations
import sqlite3
from datetime import datetime
from .foundation import bind_legacy_entity

def add(c:sqlite3.Connection,title:str,starts_at:datetime,ends_at:datetime|None=None,domain:str="life")->int:
    if not title.strip() or (ends_at and ends_at < starts_at): raise ValueError("invalid commitment")
    start=starts_at.isoformat(); end=ends_at.isoformat() if ends_at else None
    cur=c.execute("INSERT INTO commitments(title,starts_at,ends_at,domain) VALUES(?,?,?,?)",(title.strip(),start,end,domain))
    commitment_id=int(cur.lastrowid)
    bind_legacy_entity(c,table_name="commitments",row_id=commitment_id,entity_type="commitment",domain_key=domain,title=title.strip(),status="scheduled",due_at=start,metadata={"ends_at":end})
    c.commit(); return commitment_id

def for_day(c:sqlite3.Connection,day)->list[sqlite3.Row]:
    prefix=day.isoformat()+"%"
    return c.execute("SELECT * FROM commitments WHERE starts_at LIKE ? AND status='scheduled' ORDER BY starts_at",(prefix,)).fetchall()
