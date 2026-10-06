"""Goal progress derived from linked tasks."""
from __future__ import annotations
import sqlite3

def progress(c:sqlite3.Connection,goal_id:int)->dict:
    row=c.execute("SELECT id,title,status FROM goals WHERE id=?",(goal_id,)).fetchone()
    if not row: raise ValueError("goal not found")
    counts=c.execute("SELECT COUNT(*) total,SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done FROM tasks WHERE goal_id=?",(goal_id,)).fetchone()
    total=counts["total"]; done=counts["done"] or 0
    return {"id":row["id"],"title":row["title"],"done":done,"total":total,"percent":round(done*100/total,1) if total else 0.0}
