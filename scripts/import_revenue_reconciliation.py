"""Apply a connector-produced commercial snapshot after a verified backup."""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life_os.backup import create_backup
from life_os.queue import initialize_queue
from life_os.revenue_followthrough import reconcile
from life_os.revenue_reconciliation import MAX_BUNDLE_BYTES, apply_reconciliation, normalize_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    with args.bundle.open("rb") as source:
        raw = source.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("Input bundle exceeds 64 KiB")
    bundle = json.loads(raw.decode("utf-8"))
    normalize_bundle(bundle)
    # mode=rw must never create a second, empty database by accident.
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        backup = create_backup(connection, args.backup_dir)
        result = apply_reconciliation(connection, bundle)
        initialize_queue(connection)
        result["followthrough"] = reconcile(connection)
    print(json.dumps({**result, "backup": str(backup)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
