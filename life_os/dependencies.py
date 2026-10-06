"""External dependency and recovery inventory. Never store secrets here."""
from __future__ import annotations
import sqlite3
from .foundation import bind_legacy_entity

def add(c:sqlite3.Connection,name:str,category:str,recurring_cost_cents:int=0,failure_mode:str="",recovery_method:str="")->int:
    if not name.strip() or not category.strip() or recurring_cost_cents<0: raise ValueError("invalid dependency")
    cur=c.execute("INSERT INTO dependencies(name,category,recurring_cost_cents,failure_mode,recovery_method) VALUES(?,?,?,?,?)",(name.strip(),category.strip(),recurring_cost_cents,failure_mode,recovery_method))
    dependency_id=int(cur.lastrowid)
    bind_legacy_entity(c,table_name="dependencies",row_id=dependency_id,entity_type="dependency",domain_key="dependencies_vendors",title=name.strip(),cost_cents=recurring_cost_cents,metadata={"category":category.strip(),"failure_mode":failure_mode,"recovery_method":recovery_method})
    c.commit(); return dependency_id

def monthly_cost(c:sqlite3.Connection)->int:
    return c.execute("SELECT COALESCE(SUM(recurring_cost_cents),0) FROM dependencies WHERE status='active'").fetchone()[0]
