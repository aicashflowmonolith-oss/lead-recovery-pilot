"""Inventory important life resources without storing credentials."""
from __future__ import annotations
import sqlite3
from .foundation import bind_legacy_entity

def add(c:sqlite3.Connection,domain:str,name:str,kind:str,note:str="")->int:
    if not all(x.strip() for x in (domain,name,kind)): raise ValueError("domain, name and kind required")
    cur=c.execute("INSERT INTO resources(domain,name,kind,note) VALUES(?,?,?,?)",(domain.strip(),name.strip(),kind.strip(),note))
    resource_id=int(cur.lastrowid)
    bind_legacy_entity(c,table_name="resources",row_id=resource_id,entity_type="resource",domain_key=domain,title=name.strip(),metadata={"kind":kind.strip(),"note":note})
    c.commit(); return resource_id

def list_domain(c:sqlite3.Connection,domain:str):
    return c.execute("SELECT * FROM resources WHERE domain=? AND status='active' ORDER BY kind,name",(domain,)).fetchall()
