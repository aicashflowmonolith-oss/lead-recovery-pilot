"""Customer orders and payment evidence in the existing local ledger.

Recording a receipt never initiates a payment or grants spending authority.
"""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

NON_REVENUE_KINDS = {"lead", "reply", "booking", "invoice", "promise", "quote", "proposal"}
PAYMENT_KINDS = {"payment", "refund", "fee", "expense", "payout", "transfer", "notification", "registration"}
STATUSES = {"pending", "confirmed", "failed", "reversed"}
MAX_CENTS = 1_000_000_000_000
COLLECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue_orders (
    order_ref TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    scope TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    tax_cents INTEGER NOT NULL CHECK(tax_cents >= 0 AND tax_cents <= amount_cents),
    currency TEXT NOT NULL,
    acceptance_ref TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivery_ref TEXT,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS revenue_receipts (
    payment_evidence_id INTEGER PRIMARY KEY REFERENCES payment_evidence(id),
    order_ref TEXT NOT NULL REFERENCES revenue_orders(order_ref),
    evidence_ref TEXT NOT NULL,
    verification TEXT NOT NULL,
    bank_available INTEGER NOT NULL CHECK(bank_available IN (0,1)),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revenue_receipts_order ON revenue_receipts(order_ref);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, name: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


def _cents(value: Any, name: str = "amount_cents", minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_CENTS:
        raise ValueError(f"{name} must be whole cents between {minimum} and {MAX_CENTS}")
    return value


def _currency(value: Any) -> str:
    result = _text(value, "currency", 3).upper()
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise ValueError("currency must be a three-letter code")
    return result


def _timestamp(value: Any) -> str:
    value = _text(value, "timestamp", 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("timestamp must include its timezone and cannot be in the future")
    return parsed.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    result = json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(result.encode("utf-8")) > 16384:
        raise ValueError("payment metadata exceeds 16 KiB")
    return result


def _event(c: sqlite3.Connection, kind: str, payload: dict) -> None:
    # Deliberately no commit: evidence, matching and audit must commit together.
    c.execute("INSERT INTO events(kind,occurred_at,payload_json) VALUES(?,?,?)", (kind, _now(), _json(payload)))


def initialize_collection(c: sqlite3.Connection) -> None:
    c.executescript(COLLECTION_SCHEMA)


def validate_payment_evidence(*, provider: str, external_event_id: str, evidence_kind: str,
                              amount_cents: int, currency: str, status: str, observed_at: str,
                              authoritative: bool, payload: dict | None = None) -> dict:
    kind = _text(evidence_kind, "evidence_kind", 32).lower()
    if kind not in PAYMENT_KINDS:
        raise ValueError(f"{kind} is not a supported payment evidence kind")
    if status not in STATUSES or type(authoritative) is not bool:
        raise ValueError("payment status or authoritative flag is invalid")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("payment metadata must be an object")
    return dict(provider=_text(provider, "provider", 80).lower(), external_event_id=_text(external_event_id, "external_event_id"),
                evidence_kind=kind, amount_cents=_cents(amount_cents), currency=_currency(currency),
                status=status, observed_at=_timestamp(observed_at), authoritative=authoritative,
                payload_json=_json(payload or {}))


def _record_payment(c: sqlite3.Connection, values: dict) -> tuple[int, bool, bool]:
    c.execute("UPDATE payment_evidence SET id=id WHERE provider=? AND external_event_id=?", (values["provider"], values["external_event_id"]))
    previous = c.execute("SELECT * FROM payment_evidence WHERE provider=? AND external_event_id=?",
                         (values["provider"], values["external_event_id"])).fetchone()
    created = previous is None
    changed = False
    if previous is not None:
        for key in ("evidence_kind", "amount_cents", "currency"):
            if previous[key] != values[key]:
                raise ValueError("Conflicting reuse of a payment transaction reference")
        changed = previous["status"] != values["status"] or bool(previous["authoritative"]) != values["authoritative"]
        if changed:
            forward = (previous["status"] == "pending" and values["status"] in {"confirmed", "failed"}) or (previous["status"] == "confirmed" and values["status"] == "reversed") or (previous["status"] == values["status"] and not previous["authoritative"] and values["authoritative"])
            if not forward or values["observed_at"] < _timestamp(previous["observed_at"]):
                raise ValueError("Payment evidence cannot move backwards or use older evidence")
            c.execute("UPDATE payment_evidence SET status=?,authoritative=?,observed_at=?,payload_json=? WHERE id=?",
                      (values["status"], int(values["authoritative"]), values["observed_at"], values["payload_json"], previous["id"]))
        evidence_id = int(previous["id"])
    else:
        cursor = c.execute("""INSERT INTO payment_evidence(provider,external_event_id,evidence_kind,amount_cents,
            currency,status,authoritative,observed_at,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            tuple(values[k] for k in ("provider", "external_event_id", "evidence_kind", "amount_cents", "currency", "status", "authoritative", "observed_at", "payload_json")) + (_now(),))
        evidence_id = int(cursor.lastrowid)
    if created or changed:
        _event(c, "payment_evidence.recorded" if created else "payment_evidence.updated",
               {"payment_evidence_id": evidence_id, "provider": values["provider"], "status": values["status"], "authoritative": values["authoritative"]})
    fingerprint = f"verified_revenue:{values['provider']}:{values['external_event_id']}"
    emitted = False
    if values["authoritative"] and values["status"] == "confirmed" and values["evidence_kind"] == "payment":
        cursor = c.execute("""INSERT OR IGNORE INTO owner_attention(fingerprint,kind,severity,source,correlation_id,payload_json,created_at)
            VALUES(?,'verified_revenue','important',?,?,?,?)""", (fingerprint, f"payment:{values['provider']}", values["external_event_id"],
            _json({k: values[k] for k in ("provider", "amount_cents", "currency", "observed_at")}), _now()))
        emitted = cursor.rowcount == 1
        if emitted:
            _event(c, "owner_attention.created", {"attention_id": cursor.lastrowid, "kind": "verified_revenue", "source": f"payment:{values['provider']}"})
    elif values["status"] == "reversed":
        c.execute("UPDATE owner_attention SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE fingerprint=?", (_now(), fingerprint))
    return evidence_id, created, emitted


def record_payment_evidence(connection: sqlite3.Connection, **kwargs) -> tuple[int, bool, bool]:
    values = validate_payment_evidence(**kwargs)
    with connection:
        return _record_payment(connection, values)


def list_payment_evidence(connection: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("payment result limit must be 1..200")
    return [dict(r) for r in connection.execute("""SELECT id,provider,external_event_id,evidence_kind,amount_cents,currency,
        status,authoritative,observed_at,created_at FROM payment_evidence ORDER BY observed_at DESC,id DESC LIMIT ?""", (limit,))]


def record_order(c: sqlite3.Connection, *, order_ref: str, customer: str, scope: str,
                 amount_cents: int, tax_cents: int, currency: str, acceptance_ref: str, accepted_at: str) -> bool:
    """Record an evidenced agreement, never a proposal or inferred sale."""
    data = dict(order_ref=_text(order_ref, "order_ref", 100), customer=_text(customer, "customer"), scope=_text(scope, "scope", 4000),
                amount_cents=_cents(amount_cents, minimum=1), tax_cents=_cents(tax_cents, "tax_cents"), currency=_currency(currency),
                acceptance_ref=_text(acceptance_ref, "acceptance_ref", 500), accepted_at=_timestamp(accepted_at))
    if data["tax_cents"] > data["amount_cents"]:
        raise ValueError("tax cannot exceed the agreed total")
    with c:
        old = c.execute("SELECT * FROM revenue_orders WHERE order_ref=?", (data["order_ref"],)).fetchone()
        if old:
            if any(old[k] != v for k, v in data.items()):
                raise ValueError("Order reference already belongs to a different agreement")
            return False
        c.execute("""INSERT INTO revenue_orders(order_ref,customer,scope,amount_cents,tax_cents,currency,acceptance_ref,accepted_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", tuple(data.values()) + (_now(),))
        _event(c, "revenue.order.recorded", {"order_ref": data["order_ref"], "currency": data["currency"], "amount_cents": data["amount_cents"]})
    return True


def record_receipt(c: sqlite3.Connection, *, order_ref: str, provider: str, external_event_id: str,
                   evidence_kind: str, amount_cents: int, currency: str, observed_at: str,
                   evidence_ref: str, bank_record_checked: bool, bank_available: bool = False) -> tuple[int, bool, bool]:
    """Match a checked bank/processor record. A mail notice is insufficient.

    This trusted local entry point records an owner's or authorized operator's
    evidence check; it does not authenticate an account or inspect a bank itself.
    """
    order_ref = _text(order_ref, "order_ref", 100)
    evidence_ref = _text(evidence_ref, "evidence_ref", 500)
    if bank_record_checked is not True or type(bank_available) is not bool:
        raise ValueError("Check the actual bank/processor record before confirming a receipt")
    if evidence_kind not in {"payment", "refund", "fee", "expense"}:
        raise ValueError("Only customer payments, refunds, fees and actual delivery costs match orders")
    _cents(amount_cents, minimum=1)
    if bank_available and evidence_kind != "payment":
        raise ValueError("Only a payment receipt may be marked available at the bank")
    values = validate_payment_evidence(provider=provider, external_event_id=external_event_id, evidence_kind=evidence_kind,
        amount_cents=amount_cents, currency=currency, status="confirmed", observed_at=observed_at, authoritative=True,
        payload={"evidence_ref": evidence_ref, "verification": "operator_checked_record", "order_ref": order_ref})
    with c:
        # Acquire the write lock before matching; concurrent imports cannot both
        # pass the paid-order/refund-total checks.
        c.execute("UPDATE revenue_orders SET order_ref=order_ref WHERE order_ref=?", (order_ref,))
        order = c.execute("SELECT * FROM revenue_orders WHERE order_ref=?", (order_ref,)).fetchone()
        if order is None:
            raise ValueError("Record the customer's accepted order before matching payment")
        if order["currency"] != values["currency"]:
            raise ValueError("Receipt currency differs from the accepted order")
        existing = c.execute("""SELECT r.*,p.id,p.evidence_kind,p.amount_cents,p.currency,p.status,p.authoritative
            FROM payment_evidence p LEFT JOIN revenue_receipts r ON r.payment_evidence_id=p.id
            WHERE p.provider=? AND p.external_event_id=?""", (values["provider"], values["external_event_id"])).fetchone()
        if existing and existing["order_ref"]:
            if existing["order_ref"] != order_ref or existing["evidence_kind"] != evidence_kind or existing["amount_cents"] != amount_cents or existing["currency"] != values["currency"]:
                raise ValueError("Receipt already matched with different terms or order")
            if existing["status"] != "confirmed" or not existing["authoritative"]:
                raise ValueError("Receipt is no longer confirmed; investigate its provider state")
            if bool(existing["bank_available"]) != bank_available or existing["evidence_ref"] != evidence_ref:
                raise ValueError("Conflicting receipt replay; preserve the original evidence")
            return int(existing["id"]), False, False
        totals = dict(c.execute("""SELECT p.evidence_kind,SUM(p.amount_cents) FROM revenue_receipts r JOIN payment_evidence p ON p.id=r.payment_evidence_id
            WHERE r.order_ref=? AND p.authoritative=1 AND p.status='confirmed' GROUP BY p.evidence_kind""", (order_ref,)))
        if evidence_kind == "payment" and (amount_cents != order["amount_cents"] or totals.get("payment", 0)):
            raise ValueError("This collection path requires one payment for the exact accepted total; partial/extra payments need reconciliation")
        if evidence_kind == "refund" and totals.get("refund", 0) + amount_cents > totals.get("payment", 0):
            raise ValueError("Refund exceeds the confirmed payments for this order")
        result = _record_payment(c, values)
        c.execute("INSERT INTO revenue_receipts VALUES(?,?,?,?,?,?)", (result[0], order_ref, evidence_ref, "operator_checked_record", int(bank_available), _now()))
        _event(c, "revenue.receipt.matched", {"order_ref": order_ref, "payment_evidence_id": result[0], "bank_available": bank_available})
    return result


def record_delivery(c: sqlite3.Connection, *, order_ref: str, evidence_ref: str, delivered_at: str) -> bool:
    order_ref, evidence_ref, delivered_at = _text(order_ref, "order_ref", 100), _text(evidence_ref, "delivery evidence", 500), _timestamp(delivered_at)
    with c:
        row = c.execute("SELECT delivery_ref,delivered_at FROM revenue_orders WHERE order_ref=?", (order_ref,)).fetchone()
        if row is None:
            raise ValueError("Unknown order")
        if row["delivery_ref"]:
            if (row["delivery_ref"], row["delivered_at"]) != (evidence_ref, delivered_at):
                raise ValueError("Delivery already recorded with different evidence")
            return False
        c.execute("UPDATE revenue_orders SET delivery_ref=?,delivered_at=? WHERE order_ref=?", (evidence_ref, delivered_at, order_ref))
        _event(c, "revenue.delivery.recorded", {"order_ref": order_ref})
    return True


def collection_snapshot(c: sqlite3.Connection) -> dict:
    """Bounded detail with complete currency-separated totals; no bank balance inference."""
    route = c.execute("""SELECT f.value_json,f.metadata_json FROM reality_facts f JOIN reality_sources s ON s.source_key=f.source_key
        WHERE f.fact_key='money.collection_route' AND f.stale=0 AND s.enabled=1""").fetchone()
    orders = [dict(r) for r in c.execute("SELECT * FROM revenue_orders ORDER BY created_at DESC LIMIT 50")]
    totals = [dict(r) for r in c.execute("""SELECT p.currency,p.evidence_kind,SUM(p.amount_cents) AS amount_cents,COUNT(*) AS count
        FROM payment_evidence p JOIN revenue_receipts r ON r.payment_evidence_id=p.id
        WHERE p.authoritative=1 AND p.status='confirmed' GROUP BY p.currency,p.evidence_kind ORDER BY p.currency,p.evidence_kind""")]
    receipts = [dict(r) for r in c.execute("""SELECT r.order_ref,r.evidence_ref,r.verification,r.bank_available,p.provider,p.external_event_id,
        p.evidence_kind,p.amount_cents,p.currency,p.status,p.observed_at FROM revenue_receipts r JOIN payment_evidence p ON p.id=r.payment_evidence_id
        ORDER BY r.recorded_at DESC LIMIT 50""")]
    for order in orders:
        sums = dict(c.execute("""SELECT p.evidence_kind,SUM(p.amount_cents) FROM revenue_receipts r JOIN payment_evidence p ON p.id=r.payment_evidence_id
            WHERE r.order_ref=? AND p.authoritative=1 AND p.status='confirmed' GROUP BY p.evidence_kind""", (order["order_ref"],)))
        order["collected_cents"] = sums.get("payment", 0)
        order["refunded_cents"] = sums.get("refund", 0)
        order["payment_state"] = "refunded" if sums.get("refund", 0) >= order["amount_cents"] else "partly refunded" if sums.get("refund", 0) else "paid" if sums.get("payment", 0) == order["amount_cents"] else "awaiting payment"
    return {"route": json.loads(route["value_json"]) if route else None, "orders": orders, "receipts": receipts, "totals": totals,
            "order_count": c.execute("SELECT COUNT(*) FROM revenue_orders").fetchone()[0],
            "unmatched_payment_count": c.execute("""SELECT COUNT(*) FROM payment_evidence p LEFT JOIN revenue_receipts r ON r.payment_evidence_id=p.id
                WHERE r.payment_evidence_id IS NULL AND p.evidence_kind='payment' AND p.authoritative=1 AND p.status='confirmed'""").fetchone()[0],
            "bank_balance": None, "spending_authorized": False,
            "profit_status": "Not calculated: tax, delivery costs, outstanding obligations and current bank availability need reconciliation."}
