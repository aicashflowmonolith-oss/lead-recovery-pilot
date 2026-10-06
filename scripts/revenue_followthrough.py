"""Private connector handoff using the existing LIFE OS database only."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life_os.backup import create_backup
from life_os.revenue_followthrough import apply_assessment, normalize_assessment, connector_handoff


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending")
    apply = sub.add_parser("apply")
    apply.add_argument("--bundle", type=Path, required=True)
    apply.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    mode = "ro" if args.command == "pending" else "rw"
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        if args.command == "pending":
            result = connector_handoff(c)
        else:
            with args.bundle.open("rb") as source:
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError("Assessment exceeds 16 KiB")
            bundle = json.loads(raw.decode("utf-8"))
            normalize_assessment(bundle)
            backup = create_backup(c, args.backup_dir)
            result = {**apply_assessment(c, bundle), "backup": str(backup)}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
