"""Import a private LIFE OS known-state bundle with a verified pre-import backup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from life_os.backup import create_backup
from life_os.db import connect, initialize
from life_os.known_state import import_bundle


def _load(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()], "prior_context"
    payload = json.loads(text)
    if isinstance(payload, list):
        return payload, "prior_context"
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("bundle must be JSONL, a JSON list, or an object containing records")
    return payload["records"], str(payload.get("source_label") or "prior_context")


def main() -> int:
    home = Path.home() / ".life-os"
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=home / "life.db")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, default=home / "backups")
    args = parser.parse_args()

    records, source_label = _load(args.bundle)
    connection = connect(args.db)
    try:
        initialize(connection)
        backup = create_backup(connection, args.backup_dir)
        result = import_bundle(connection, records, source_label=source_label)
        print(json.dumps({"backup": str(backup), **result}, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
