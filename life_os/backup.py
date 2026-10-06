"""Small verified SQLite backups."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime,timezone

def create_backup(source:sqlite3.Connection,directory:str|Path)->Path:
    destdir=Path(directory); destdir.mkdir(parents=True,exist_ok=True)
    path=destdir/f"life-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    target=sqlite3.connect(path)
    try: source.backup(target)
    finally: target.close()
    verify=sqlite3.connect(path)
    try:
        result=verify.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok": raise RuntimeError(f"backup integrity check failed: {result}")
    finally: verify.close()
    return path
