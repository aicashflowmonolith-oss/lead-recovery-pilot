"""Portable JSON export of non-secret LIFE OS state."""
from __future__ import annotations
import json,sqlite3
from pathlib import Path
from .privacy import redact

TABLES=("profile","goals","tasks","routines","accounts","transactions","purchases","metrics","commitments","checkins","events")

def export_json(c:sqlite3.Connection,path:str|Path)->Path:
    data={}
    for table in TABLES:
        rows=[dict(r) for r in c.execute(f"SELECT * FROM {table}")]
        if table=="profile":
            sensitive={"address","password","token","secret","api_key","recovery_code","health_note"}
            for row in rows:
                if row.get("key","").lower() in sensitive: row["value"]="[REDACTED]"
        data[table]=rows
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(redact(data),indent=2,sort_keys=True),encoding="utf-8")
    return p
