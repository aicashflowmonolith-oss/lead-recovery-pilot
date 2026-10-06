"""Purchase queue, classification, milestones, and affordability controls."""
from __future__ import annotations

import sqlite3

from .foundation import bind_legacy_entity

BUCKET_SHARED = "shared"
BUCKET_MONOLITH = "monolith"
BUCKET_PERSONAL = "personal"
VALID_BUCKETS = {BUCKET_SHARED, BUCKET_MONOLITH, BUCKET_PERSONAL}

MILESTONES = (
    (1, "phone_service", "Unlocked dual-SIM/eSIM phone + continuously active cellular service", BUCKET_SHARED,
     "Critical shared infrastructure for personal use and MONOLITH identity, authentication, communications, hotspot, banking, and remote control. Prefer carrier redundancy where practical."),
    (2, "monolith_blockers", "MONOLITH revenue blockers and infrastructure", BUCKET_MONOLITH,
     "Remove or route around blockers that directly prevent revenue or system reliability."),
    (3, "transportation", "Reliable, fully legal primary transportation", BUCKET_PERSONAL,
     "Primary transport comes before toys. Do not permanently lock a model: re-source and re-verify the actual candidate at commitment time for reliability, recoverability, legal/insurance fit, ownership cost, parts/service, owner fit, utility, enjoyment, and current value."),
    (4, "revenue_upgrades", "Revenue-producing hardware/software upgrades", BUCKET_MONOLITH,
     "Only upgrades with a clear path to improving revenue capacity, reliability, or throughput."),
    (5, "personal_upgrades", "Nonessential personal upgrades", BUCKET_PERSONAL,
     "Comfort, entertainment, appearance, room/audio, and other discretionary purchases."),
    (6, "dream_bike", "Bonnell 902 dream-bike fund", BUCKET_PERSONAL,
     "Long-term reward target; reassess the market before purchase and do not impair operating capital or emergency reserves."),
)


def initialize(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS purchase_policy (
            purchase_id INTEGER PRIMARY KEY REFERENCES purchases(id) ON DELETE CASCADE,
            bucket TEXT NOT NULL CHECK(bucket IN ('shared','monolith','personal')),
            ladder_rank INTEGER NOT NULL CHECK(ladder_rank BETWEEN 1 AND 6),
            auto_classified INTEGER NOT NULL DEFAULT 1 CHECK(auto_classified IN (0,1)),
            reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS procurement_milestones (
            rank INTEGER PRIMARY KEY CHECK(rank BETWEEN 1 AND 6),
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            bucket TEXT NOT NULL CHECK(bucket IN ('shared','monolith','personal')),
            state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('active','queued','done','blocked')),
            note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    for rank, code, title, bucket, note in MILESTONES:
        c.execute(
            """INSERT INTO procurement_milestones(rank,code,title,bucket,state,note)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(rank) DO UPDATE SET
                 code=excluded.code,title=excluded.title,bucket=excluded.bucket,note=excluded.note""",
            (rank, code, title, bucket, "active" if rank == 1 else "queued", note),
        )
    rows = c.execute(
        """SELECT p.id,p.title,p.necessity,p.note
           FROM purchases p LEFT JOIN purchase_policy pp ON pp.purchase_id=p.id
           WHERE pp.purchase_id IS NULL"""
    ).fetchall()
    for row in rows:
        bucket, ladder_rank, reason = classify_purchase(row["title"], row["note"], bool(row["necessity"]))
        c.execute(
            "INSERT OR REPLACE INTO purchase_policy(purchase_id,bucket,ladder_rank,auto_classified,reason) VALUES(?,?,?,?,?)",
            (row["id"], bucket, ladder_rank, 1, reason),
        )
    c.commit()


def classify_purchase(title: str, note: str = "", necessity: bool = False) -> tuple[str, int, str]:
    text = f"{title} {note}".lower()
    if "bonnell 902" in text or ("dream" in text and "bike" in text):
        return BUCKET_PERSONAL, 6, "dream-bike milestone"
    shared_phone_terms = (
        "phone", "smartphone", "iphone", "pixel", "galaxy", "esim", "cellular",
        "carrier", "mobile plan", "phone plan", "data plan", "sim card", "dual-sim", "dual sim", "authenticator", "hotspot",
    )
    if any(term in text for term in shared_phone_terms):
        return BUCKET_SHARED, 1, "shared phone/service infrastructure"
    revenue_blocker_terms = (
        "revenue blocker", "blocker", "business number", "payment processor",
        "domain renewal", "hosting outage", "service outage", "credential",
    )
    if any(term in text for term in revenue_blocker_terms):
        return BUCKET_MONOLITH, 2, "MONOLITH revenue blocker/infrastructure"
    transport_terms = (
        "transport", "bus pass", "transit", "bicycle", "ebike", "e-bike", "scooter",
        "vehicle", "longboard", "car", "moped",
    )
    if any(term in text for term in transport_terms):
        return BUCKET_PERSONAL, 3, "transportation milestone"
    monolith_terms = (
        "ram", "ssd", "computer", "pc ", "laptop", "server", "domain", "hosting",
        "software", "subscription", "automation", "monitor", "router", "backup drive",
        "storage", "github", "railway", "supabase",
    )
    if any(term in text for term in monolith_terms):
        return BUCKET_MONOLITH, 4, "revenue-producing MONOLITH upgrade"
    if necessity:
        return BUCKET_PERSONAL, 3, "essential personal need"
    return BUCKET_PERSONAL, 5, "nonessential personal purchase"


def add_purchase(
    c: sqlite3.Connection,
    title: str,
    price_cents: int,
    priority: int = 50,
    necessity: bool = False,
    note: str = "",
    *,
    bucket: str | None = None,
    ladder_rank: int | None = None,
) -> int:
    if not title.strip() or price_cents < 0 or not 0 <= priority <= 100:
        raise ValueError("invalid purchase")
    auto = bucket is None or ladder_rank is None
    if auto:
        bucket, ladder_rank, reason = classify_purchase(title, note, necessity)
    else:
        if bucket not in VALID_BUCKETS or not 1 <= int(ladder_rank) <= 6:
            raise ValueError("invalid purchase policy")
        reason = "manually classified"
    cur = c.execute(
        "INSERT INTO purchases(title,price_cents,priority,necessity,note) VALUES(?,?,?,?,?)",
        (title.strip(), price_cents, priority, int(necessity), note),
    )
    purchase_id = int(cur.lastrowid)
    c.execute(
        "INSERT INTO purchase_policy(purchase_id,bucket,ladder_rank,auto_classified,reason) VALUES(?,?,?,?,?)",
        (purchase_id, bucket, int(ladder_rank), int(auto), reason),
    )
    bind_legacy_entity(
        c,
        table_name="purchases",
        row_id=purchase_id,
        entity_type="purchase",
        domain_key="procurement",
        title=title.strip(),
        status="wanted",
        priority=priority,
        cost_cents=price_cents,
        metadata={
            "necessity": bool(necessity),
            "note": note,
            "bucket": bucket,
            "ladder_rank": int(ladder_rank),
        },
    )
    c.commit()
    return purchase_id


def queue(c: sqlite3.Connection) -> list[sqlite3.Row]:
    initialize(c)
    return c.execute(
        """SELECT p.*,pp.bucket,pp.ladder_rank,pp.auto_classified,pp.reason AS classification_reason
           FROM purchases p JOIN purchase_policy pp ON pp.purchase_id=p.id
           WHERE p.status='wanted'
           ORDER BY pp.ladder_rank ASC,p.necessity DESC,p.priority DESC,p.price_cents ASC,p.id"""
    ).fetchall()


def procurement_plan(c: sqlite3.Connection) -> list[sqlite3.Row]:
    initialize(c)
    return c.execute(
        "SELECT rank,code,title,bucket,state,note FROM procurement_milestones ORDER BY rank"
    ).fetchall()


def active_milestone(c: sqlite3.Connection) -> sqlite3.Row | None:
    initialize(c)
    return c.execute(
        "SELECT rank,code,title,bucket,state,note FROM procurement_milestones WHERE state='active' ORDER BY rank LIMIT 1"
    ).fetchone()


def set_milestone_state(c: sqlite3.Connection, code: str, state: str) -> None:
    if state not in {"active", "queued", "done", "blocked"}:
        raise ValueError("invalid milestone state")
    row = c.execute("SELECT rank FROM procurement_milestones WHERE code=?", (code,)).fetchone()
    if row is None:
        raise ValueError("unknown milestone")
    c.execute("UPDATE procurement_milestones SET state=? WHERE code=?", (state, code))
    if state == "done":
        next_row = c.execute(
            "SELECT rank,code FROM procurement_milestones WHERE rank>? AND state='queued' ORDER BY rank LIMIT 1",
            (row["rank"],),
        ).fetchone()
        if next_row:
            c.execute("UPDATE procurement_milestones SET state='active' WHERE code=?", (next_row["code"],))
    c.commit()


def affordable(c: sqlite3.Connection, reserve_cents: int = 0) -> list[sqlite3.Row]:
    cash = sum(r["balance_cents"] for r in c.execute("SELECT balance_cents FROM accounts"))
    budget = max(0, cash - reserve_cents)
    out: list[sqlite3.Row] = []
    for p in queue(c):
        if p["price_cents"] <= budget:
            out.append(p)
            budget -= p["price_cents"]
    return out