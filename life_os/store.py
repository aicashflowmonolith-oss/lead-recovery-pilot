"""Repository layer for LIFE OS."""
from __future__ import annotations
import sqlite3
from datetime import date
from .models import Goal, Task
from .foundation import bind_legacy_entity

def set_profile(c: sqlite3.Connection, key: str, value: str) -> None:
    if not key.strip(): raise ValueError("profile key must not be empty")
    c.execute("INSERT INTO profile(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key.strip(),value))
    c.commit()

def get_profile(c: sqlite3.Connection) -> dict[str,str]:
    return {r["key"]:r["value"] for r in c.execute("SELECT key,value FROM profile ORDER BY key")}

def add_goal(c: sqlite3.Connection,title:str,priority:int=50)->Goal:
    if not title.strip(): raise ValueError("goal title must not be empty")
    if not 0 <= priority <= 100: raise ValueError("priority must be 0..100")
    cur=c.execute("INSERT INTO goals(title,priority,status) VALUES(?,?,'active')",(title.strip(),priority))
    goal_id=int(cur.lastrowid)
    bind_legacy_entity(c,table_name="goals",row_id=goal_id,entity_type="goal",domain_key="goals",title=title.strip(),status="active",priority=priority)
    c.commit()
    return Goal(goal_id,title.strip(),priority,"active")

def list_goals(c:sqlite3.Connection)->list[Goal]:
    return [Goal(r["id"],r["title"],r["priority"],r["status"]) for r in c.execute("SELECT * FROM goals ORDER BY priority DESC,id")]

def add_task(c:sqlite3.Connection,title:str,priority:int=50,effort_minutes:int=30,due_date:date|None=None,goal_id:int|None=None)->Task:
    if not title.strip(): raise ValueError("task title must not be empty")
    if not 0 <= priority <= 100: raise ValueError("priority must be 0..100")
    if effort_minutes < 1: raise ValueError("effort must be positive")
    due=due_date.isoformat() if due_date else None
    cur=c.execute("INSERT INTO tasks(title,priority,effort_minutes,due_date,status,goal_id) VALUES(?,?,?,?, 'open',?)",(title.strip(),priority,effort_minutes,due,goal_id))
    task_id=int(cur.lastrowid)
    bind_legacy_entity(c,table_name="tasks",row_id=task_id,entity_type="task",domain_key="goals",title=title.strip(),status="open",priority=priority,due_at=due,metadata={"goal_id":goal_id,"effort_minutes":effort_minutes})
    c.commit()
    return Task(task_id,title.strip(),priority,effort_minutes,due_date,"open",goal_id)

def list_open_tasks(c:sqlite3.Connection)->list[Task]:
    rows=c.execute("SELECT * FROM tasks WHERE status='open'")
    return [Task(r["id"],r["title"],r["priority"],r["effort_minutes"],date.fromisoformat(r["due_date"]) if r["due_date"] else None,r["status"],r["goal_id"]) for r in rows]

def complete_task(c:sqlite3.Connection,task_id:int)->bool:
    cur=c.execute("UPDATE tasks SET status='done',completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'",(task_id,))
    if cur.rowcount==1:
        row=c.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        bind_legacy_entity(c,table_name="tasks",row_id=task_id,entity_type="task",domain_key="goals",title=row["title"],status=row["status"],priority=row["priority"],due_at=row["due_date"],metadata={"goal_id":row["goal_id"],"effort_minutes":row["effort_minutes"]})
    c.commit()
    return cur.rowcount==1
