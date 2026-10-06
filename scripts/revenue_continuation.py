"""Bounded handoff for the persistent revenue connector. Never creates a database."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life_os import revenue_continuation as loop
from life_os.backup import create_backup
from life_os.queue import set_state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("operation", choices=("status", "enable", "disable", "complete"))
    p.add_argument("--bundle", type=Path)
    args = p.parse_args()
    mode = "ro" if args.operation == "status" else "rw"
    c = sqlite3.connect(args.db.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        if args.operation == "status":
            result = loop.status(c)
        else:
            bundle = None
            if args.operation == "complete":
                if args.bundle is None:
                    p.error("--bundle required")
                with args.bundle.open("rb") as f:
                    raw = f.read(16385)
                if len(raw) > 16384:
                    raise ValueError("receipt exceeds 16 KiB")
                bundle = json.loads(raw.decode("utf-8-sig"))
            backup = create_backup(c, args.db.parent / "backups" / "revenue-continuation")
            if args.operation == "complete":
                result = loop.complete(c, bundle)
            else:
                set_state(c, loop.ENABLED, "1" if args.operation == "enable" else "0")
                result = loop.reconcile(c)
            result["backup"] = str(backup)
        print(json.dumps(result, sort_keys=True))
    finally:
        c.close()


if __name__ == "__main__":
    main()
