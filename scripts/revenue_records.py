"""Bounded local order/receipt handoff. Does not connect to or transact with a bank."""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life_os.backup import create_backup
from life_os.money import collection_snapshot, record_order, record_receipt, record_delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("operation", choices=("status", "order", "receipt", "delivery"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--evidence-file", type=Path, help="Optional original receipt file to retain privately, maximum 2 MiB")
    parser.add_argument("--bank-record-checked", action="store_true", help="Only after checking the actual bank/processor record and purpose")
    args = parser.parse_args()
    data = None
    if args.operation != "status":
        if args.bundle is None:
            parser.error("--bundle is required")
        with args.bundle.open("rb") as f:
            raw = f.read(16385)
        if len(raw) > 16384:
            raise ValueError("Record exceeds 16 KiB")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or "bank_record_checked" in data:
            raise ValueError("Expected an object; bank evidence checking cannot come from the input bundle")
    db = args.db.resolve()
    mode = "ro" if args.operation == "status" else "rw"
    with closing(sqlite3.connect(db.as_uri() + "?mode=" + mode, uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        if args.operation == "status":
            result = collection_snapshot(c)
        else:
            if args.operation == "receipt" and not args.bank_record_checked:
                raise ValueError("Receipt import requires an actual bank/processor evidence check")
            backup = create_backup(c, db.parent / "backups" / "revenue-records")
            if args.evidence_file:
                if args.operation != "receipt":
                    raise ValueError("Receipt attachment only applies to receipt records")
                with args.evidence_file.open("rb") as f:
                    evidence = f.read(2097153)
                if not evidence or len(evidence) > 2097152:
                    raise ValueError("Evidence must contain 1 byte to 2 MiB")
                digest = hashlib.sha256(evidence).hexdigest()
                target = db.parent / "revenue" / "evidence" / (digest + ".bin")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    with target.open("xb") as f:
                        f.write(evidence)
                elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError("Existing evidence archive is damaged")
                data["evidence_ref"] = "sha256:" + digest
            if args.operation == "order":
                result = {"created": record_order(c, **data)}
            elif args.operation == "receipt":
                evidence_id, created, signaled = record_receipt(c, **data, bank_record_checked=True)
                result = {"payment_evidence_id": evidence_id, "created": created, "revenue_attention_created": signaled}
            else:
                result = {"created": record_delivery(c, **data)}
            result["backup"] = str(backup)
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
