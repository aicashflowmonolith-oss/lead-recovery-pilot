"""Self-audit checks for LIFE OS."""
from __future__ import annotations
import sqlite3

def run(c:sqlite3.Connection)->list[str]:
    problems=[]
    integrity=c.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity!="ok": problems.append(f"database integrity: {integrity}")
    fk=c.execute("PRAGMA foreign_key_check").fetchall()
    if fk: problems.append(f"foreign key violations: {len(fk)}")
    bad=c.execute("SELECT COUNT(*) FROM tasks WHERE priority NOT BETWEEN 0 AND 100 OR effort_minutes<=0").fetchone()[0]
    if bad: problems.append(f"invalid tasks: {bad}")
    return problems
