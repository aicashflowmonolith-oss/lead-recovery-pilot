"""Bounded maintenance child: canonical source is read-only; no ledger writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import uuid

from . import audit
from .backup import create_backup


def open_source(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    return connection


def verified_backup(connection, directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex
    staging = directory / (".maintenance-" + suffix)
    staging.mkdir()
    # Incomplete or interrupted artifacts remain in staging as evidence. They
    # cannot match the parent's daily completed-backup glob.
    verified = create_backup(connection, staging)
    published = directory / (verified.stem + "-" + suffix + ".db")
    verified.rename(published)
    staging.rmdir()
    return {"created": True, "path": str(published)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("audit", "backup"))
    parser.add_argument("--db", required=True)
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    connection = open_source(args.db)
    try:
        if args.kind == "audit":
            result = {"problems": audit.run(connection)}
        else:
            if not args.backup_dir:
                parser.error("backup directory required")
            result = verified_backup(connection, args.backup_dir)
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
