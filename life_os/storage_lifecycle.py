"""Storage lifecycle reporting and bounded maintenance planning."""
from __future__ import annotations
import sqlite3
from datetime import datetime,timedelta,timezone
from .events import append_event
from .queue import set_state
def report(c:sqlite3.Connection)->dict:
    page_count=int(c.execute('PRAGMA page_count').fetchone()[0]); page_size=int(c.execute('PRAGMA page_size').fetchone()[0]); freelist=int(c.execute('PRAGMA freelist_count').fetchone()[0])
    tables={r[0]:int(c.execute('SELECT COUNT(*) FROM "'+r[0].replace('"','""')+'"').fetchone()[0]) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    result={'database_bytes':page_count*page_size,'free_pages':freelist,'page_size':page_size,'table_rows':tables}
    set_state(c,'storage.last_report',__import__('json').dumps(result,sort_keys=True)); return result
def maintenance_plan(c:sqlite3.Connection,*,event_retention_days:int=365)->dict:
    if event_retention_days<30: raise ValueError('retention floor is 30 days')
    current=report(c); cutoff=(datetime.now(timezone.utc)-timedelta(days=event_retention_days)).isoformat()
    old_events=int(c.execute('SELECT COUNT(*) FROM events WHERE occurred_at<?',(cutoff,)).fetchone()[0])
    return {'report':current,'event_retention_days':event_retention_days,'old_event_candidates':old_events,'automatic_deletion':False,'recommendations':['checkpoint WAL during maintenance windows','archive before any destructive pruning','verify backup restore before schema-changing upgrades']}
def checkpoint(c:sqlite3.Connection)->dict:
    row=c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone(); result={'busy':int(row[0]),'log_frames':int(row[1]),'checkpointed_frames':int(row[2])}; append_event(c,'storage.wal_checkpoint',result); return result
