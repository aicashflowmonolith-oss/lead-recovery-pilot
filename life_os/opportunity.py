"""Evidence-aware serendipity and opportunity engine.

Signals justify investigation, never consequential authorization.
"""
from __future__ import annotations
import hashlib, json, sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass(frozen=True)
class Opportunity:
    id:int; title:str; score:float; status:str; rationale:str

def _now()->str: return datetime.now(timezone.utc).isoformat()
def _fingerprint(kind:str,source:str,content:str)->str:
    return hashlib.sha256(f"{kind}\0{source}\0{content}".encode()).hexdigest()

def initialize(c:sqlite3.Connection)->None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS opportunity_signals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
      kind TEXT NOT NULL, source TEXT NOT NULL, source_group TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL, confidence REAL NOT NULL DEFAULT .5 CHECK(confidence BETWEEN 0 AND 1),
      novelty REAL NOT NULL DEFAULT .5 CHECK(novelty BETWEEN 0 AND 1),
      observed_at TEXT NOT NULL, provenance_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS opportunity_questions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS opportunities(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, rationale TEXT NOT NULL,
      upside REAL NOT NULL DEFAULT 0, probability REAL NOT NULL DEFAULT 0,
      information_value REAL NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
      reversibility REAL NOT NULL DEFAULT 1, novelty REAL NOT NULL DEFAULT 0,
      score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'candidate',
      consequential INTEGER NOT NULL DEFAULT 0 CHECK(consequential IN (0,1)),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS opportunity_evidence(
      opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
      signal_id INTEGER NOT NULL REFERENCES opportunity_signals(id),
      stance TEXT NOT NULL CHECK(stance IN ('support','counter','context')),
      PRIMARY KEY(opportunity_id,signal_id));
    CREATE TABLE IF NOT EXISTS opportunity_experiments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
      hypothesis TEXT NOT NULL, test TEXT NOT NULL, max_cost REAL NOT NULL DEFAULT 0,
      reversible INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'proposed',
      result TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_opportunity_signal_kind ON opportunity_signals(kind,observed_at);
    CREATE INDEX IF NOT EXISTS idx_opportunity_rank ON opportunities(status,score DESC);
    """); c.commit()

def capture_signal(c:sqlite3.Connection,kind:str,source:str,content:str,*,source_group:str="",confidence:float=.5,novelty:float=.5,provenance:dict|None=None)->int:
    if not kind.strip() or not source.strip() or not content.strip(): raise ValueError("kind, source and content required")
    if not (0<=confidence<=1 and 0<=novelty<=1): raise ValueError("confidence/novelty must be 0..1")
    fp=_fingerprint(kind.strip(),source.strip(),content.strip())
    c.execute("""INSERT OR IGNORE INTO opportunity_signals
      (fingerprint,kind,source,source_group,content,confidence,novelty,observed_at,provenance_json)
      VALUES(?,?,?,?,?,?,?,?,?)""",(fp,kind.strip(),source.strip(),source_group.strip(),content.strip(),confidence,novelty,_now(),json.dumps(provenance or {},sort_keys=True)))
    row=c.execute("SELECT id FROM opportunity_signals WHERE fingerprint=?",(fp,)).fetchone(); c.commit(); return int(row["id"])

def independent_signal_count(c:sqlite3.Connection,kind:str)->int:
    row=c.execute("""SELECT COUNT(DISTINCT CASE WHEN source_group='' THEN source ELSE source_group END)
                     FROM opportunity_signals WHERE kind=?""",(kind,)).fetchone()
    return int(row[0])

def create_opportunity(c:sqlite3.Connection,title:str,rationale:str,*,upside:float,probability:float,information_value:float,cost:float,reversibility:float,novelty:float,consequential:bool=False)->Opportunity:
    vals=(upside,probability,information_value,cost,reversibility,novelty)
    if any(v<0 for v in vals): raise ValueError("scoring inputs must be nonnegative")
    score=(upside*probability)+(information_value*.5)+(reversibility*.25)+(novelty*.15)-cost
    now=_now(); cur=c.execute("""INSERT INTO opportunities
      (title,rationale,upside,probability,information_value,cost,reversibility,novelty,score,consequential,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(title.strip(),rationale.strip(),upside,probability,information_value,cost,reversibility,novelty,score,int(consequential),now,now))
    c.commit(); return Opportunity(int(cur.lastrowid),title,score,"candidate",rationale)

def attach_evidence(c:sqlite3.Connection,opportunity_id:int,signal_id:int,stance:str)->None:
    if stance not in {"support","counter","context"}: raise ValueError("invalid stance")
    c.execute("INSERT OR REPLACE INTO opportunity_evidence VALUES(?,?,?)",(opportunity_id,signal_id,stance)); c.commit()

def propose_experiment(c:sqlite3.Connection,opportunity_id:int,hypothesis:str,test:str,*,max_cost:float=0,reversible:bool=True)->int:
    row=c.execute("SELECT consequential FROM opportunities WHERE id=?",(opportunity_id,)).fetchone()
    if not row: raise ValueError("unknown opportunity")
    status="owner_gate" if row["consequential"] or max_cost>0 or not reversible else "proposed"
    cur=c.execute("""INSERT INTO opportunity_experiments(opportunity_id,hypothesis,test,max_cost,reversible,status,created_at)
                     VALUES(?,?,?,?,?,?,?)""",(opportunity_id,hypothesis,test,max_cost,int(reversible),status,_now()))
    c.commit(); return int(cur.lastrowid)

def ranked(c:sqlite3.Connection,limit:int=20)->list[Opportunity]:
    rows=c.execute("SELECT id,title,score,status,rationale FROM opportunities WHERE status='candidate' ORDER BY score DESC,id ASC LIMIT ?",(limit,)).fetchall()
    return [Opportunity(r["id"],r["title"],r["score"],r["status"],r["rationale"]) for r in rows]
