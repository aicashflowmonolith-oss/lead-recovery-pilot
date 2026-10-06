"""Local collection page using the existing ledger and app request protections."""
from __future__ import annotations
import html
import re
from datetime import datetime
from decimal import Decimal
from .money import collection_snapshot, record_order, record_receipt, record_delivery


def _esc(value):
    return html.escape(str(value), quote=True)


def _amount(cents, currency):
    return f"{currency} {cents / 100:,.2f}"


def _cents(value):
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("Enter an amount with at most two decimal places")
    return int(Decimal(value) * 100)


def _date(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Enter the date and time shown in the evidence") from exc
    return parsed.astimezone().isoformat()


def money_action(connection, form):
    get = lambda key, default="": form.get(key, [default])[0]
    action = get("operation")
    if action == "order":
        record_order(connection, order_ref=get("order_ref"), customer=get("customer"), scope=get("scope"),
            amount_cents=_cents(get("amount")), tax_cents=_cents(get("tax", "0")), currency=get("currency", "CAD"),
            acceptance_ref=get("acceptance_ref"), accepted_at=_date(get("accepted_at")))
        return "Accepted order saved. Payment has not been assumed."
    if action == "receipt":
        record_receipt(connection, order_ref=get("order_ref"), provider=get("provider"), external_event_id=get("external_event_id"),
            evidence_kind=get("evidence_kind"), amount_cents=_cents(get("amount")), currency=get("currency", "CAD"),
            observed_at=_date(get("observed_at")), evidence_ref=get("evidence_ref"),
            bank_record_checked=get("bank_record_checked") == "1", bank_available=get("bank_available") == "1")
        return "Receipt matched and saved. Existing spending approvals still apply."
    if action == "delivery":
        record_delivery(connection, order_ref=get("order_ref"), evidence_ref=get("evidence_ref"), delivered_at=_date(get("delivered_at")))
        return "Delivery evidence saved."
    raise ValueError("Unknown collection operation")


def render_money(data, token, message=""):
    route = data.get("route") or {}
    destination = route.get("destination") or {}
    receiving = "Receiving details have not been recorded."
    if route.get("primary_method") == "interac_e_transfer" and destination.get("address"):
        receiving = (f"<strong>Interac e-Transfer</strong><p>{_esc(destination['address'])}</p>"
                     f"<p>Recipient: {_esc(destination.get('recipient_display_name') or 'Not yet confirmed')} · "
                     f"Bank: {_esc(destination.get('financial_institution') or 'Not yet recorded')}</p>"
                     f"<p>Autodeposit: {'enabled, owner confirmed' if destination.get('autodeposit_enabled') else 'not confirmed'}.</p>"
                     "<small>Saved receiving instructions. Bank access and current balance are not connected.</small>")
    labels = {"payment": "Customer payments received", "refund": "Refunds recorded", "fee": "Fees recorded", "expense": "Delivery costs recorded"}
    totals = "".join(f"<li>{_esc(labels.get(row['evidence_kind'], row['evidence_kind']))}: <strong>{_esc(_amount(row['amount_cents'], row['currency']))}</strong></li>" for row in data["totals"])
    if not totals:
        totals = "<li>No matched, confirmed customer payments recorded.</li>"
    orders = "".join(f"<article><strong>{_esc(o['order_ref'])} · {_esc(o['customer'])}</strong>"
        f"<p>{_esc(o['scope'])}</p><p>{_esc(_amount(o['amount_cents'], o['currency']))} agreed total; includes {_esc(_amount(o['tax_cents'], o['currency']))} declared tax.</p>"
        f"<p>{_esc(o['payment_state'])} · {'Delivery recorded' if o['delivery_ref'] else 'Delivery evidence not yet recorded'}</p></article>" for o in data["orders"]) or "<p>No accepted orders recorded yet. A prospect or proposal is not an order.</p>"
    receipts = "".join(f"<article><strong>{_esc(r['order_ref'])} · {_esc(r['evidence_kind'])} · {_esc(_amount(r['amount_cents'],r['currency']))}</strong>"
        f"<p>{_esc(r['provider'])} / {_esc(r['external_event_id'])} · {_esc(r['status'])}</p>"
        f"<small>Checked against: {_esc(r['evidence_ref'])}. {'Marked available at bank when checked' if r['bank_available'] else 'Bank availability not confirmed'}.</small></article>" for r in data["receipts"]) or "<p>No order receipts recorded.</p>"
    options = "".join(f"<option value='{_esc(o['order_ref'])}'>{_esc(o['order_ref'])} — {_esc(o['customer'])}</option>" for o in data["orders"])
    hidden = lambda action: f"<input type='hidden' name='csrf' value='{_esc(token)}'><input type='hidden' name='operation' value='{action}'>"
    money_fields = "<label>Amount<input name='amount' type='number' min='0.01' step='0.01' required></label><label>Currency<input name='currency' value='CAD' pattern='[A-Za-z]{3}' maxlength='3' required></label>"
    select = f"<label>Accepted order<select name='order_ref' required><option value=''>Select an order</option>{options}</select></label>"
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Customer payments · LIFE OS</title><style>
body{{font:16px system-ui,sans-serif;background:#101821;color:#edf3f8;max-width:1000px;margin:auto;padding:20px;line-height:1.5}}a{{color:#8fd6ff}}section{{padding:20px;margin:16px 0;background:#1c2937;border-radius:12px}}article{{border-top:1px solid #536373;padding:12px 0;overflow-wrap:anywhere}}p,small{{overflow-wrap:anywhere}}form{{display:grid;gap:14px;margin-top:16px}}label{{display:grid;gap:5px}}input,select,textarea,button{{box-sizing:border-box;width:100%;font:inherit;padding:10px;border-radius:6px;border:1px solid #536373;background:#111c29;color:inherit}}input[type=checkbox]{{width:auto}}button{{background:#184e6c;cursor:pointer}}summary{{cursor:pointer;font-weight:bold}}small{{color:#c3cbd4}}.message{{padding:12px;background:#243d4f}}h1{{font-size:1.8rem}}@media(max-width:500px){{body{{padding:10px}}section{{padding:14px}}}}
</style></head><body><a href='/#money'>← LIFE OS</a><h1>Customer payments</h1>
{f"<p class='message' role='status'>{_esc(message)}</p>" if message else ''}
<section><h2>How customers pay</h2>{receiving}<p>Stripe is retained for later. Existing contact, pilot and spending approvals still apply.</p></section>
<section><h2>Recorded collections</h2><ul>{totals}</ul><p>{data['unmatched_payment_count']} confirmed payment record(s) still need an order match.</p><p>These are receipt totals, not your current bank balance or profit. Tax, refunds, delivery obligations and costs must be reconciled before using earnings.</p></section>
<section><h2>Accepted orders</h2>{orders}<small>Showing up to 50 recent orders of {data['order_count']}.</small><details><summary>Record an accepted order</summary>
<form method='post' action='/money'>{hidden('order')}<label>Order reference<input name='order_ref' maxlength='100' required></label><label>Customer<input name='customer' maxlength='200' required></label><label>Agreed work<textarea name='scope' maxlength='4000' required></textarea></label>{money_fields}<label>Tax included in the agreed total<input name='tax' type='number' min='0' step='0.01' required></label><label>Evidence of customer acceptance<input name='acceptance_ref' maxlength='500' placeholder='Accepted quote, signed agreement or exact conversation reference' required></label><label>Acceptance date and time (local)<input name='accepted_at' type='datetime-local' required></label><button>Save accepted order</button></form></details></section>
<section><h2>Receipts</h2>{receipts}<details><summary>Record a checked receipt</summary><p>Use the actual bank or processor transaction. A registration email, payment notice, invoice or transfer between your own accounts is not a customer payment. This path supports one payment for the exact agreed total.</p>
<form method='post' action='/money'>{hidden('receipt')}{select}<label>Record type<select name='evidence_kind'><option value='payment'>Customer payment</option><option value='refund'>Refund</option><option value='fee'>Processing fee</option><option value='expense'>Actual delivery cost</option></select></label><label>Bank or processor<input name='provider' value='wealthsimple_interac' maxlength='80' required></label><label>Transaction reference<input name='external_event_id' maxlength='200' required></label>{money_fields}<label>Transaction date and time (local)<input name='observed_at' type='datetime-local' required></label><label>Bank or processor evidence reference<input name='evidence_ref' maxlength='500' placeholder='Statement / transaction record reference' required></label><label><span><input name='bank_record_checked' type='checkbox' value='1' required> I checked the actual transaction and its purpose against this order.</span></label><label><span><input name='bank_available' type='checkbox' value='1'> For a customer payment: the bank showed these funds as available when checked.</span></label><button>Match and record receipt</button></form></details>
<details><summary>Record delivery</summary><form method='post' action='/money'>{hidden('delivery')}{select}<label>Delivery evidence<input name='evidence_ref' maxlength='500' required></label><label>Delivery date and time (local)<input name='delivered_at' type='datetime-local' required></label><button>Save delivery evidence</button></form></details></section></body></html>"""
