"""Disposable same-request proof using the actual worker CLI, without models.

Run from a qualified checkout with --home pointing to a NEW scratch directory.
Never accepts an existing home/database; all seeded plans and injected faults
belong to this disposable fixture. Completed fixture evidence is preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from life_os import local_capability_recipes as recipes, request_fabric as fabric
from life_os.ai_cli import run_bounded
from life_os.capabilities import set_capability_health
from life_os.db import connect, initialize


def _receipts(c):
    return [tuple(row) for row in c.execute("SELECT * FROM execution_steps WHERE state='succeeded' ORDER BY request_id,ordinal")]


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    home = args.home.resolve()
    if home.exists() or not home.parent.is_dir():
        raise SystemExit("Proof requires a new scratch directory under an existing parent; existing evidence retained")
    home.mkdir()
    environment = dict(os.environ)
    empty = home / "no-model-commands"
    empty.mkdir()
    # These process-only scopes exclude npm/native model discovery. They do not
    # alter the user's credentials, PATH, CLI installation or canonical runtime.
    environment.update(APPDATA=str(empty), LOCALAPPDATA=str(empty),
                       PATH=str(Path(environment.get("SystemRoot", "C:/Windows")) / "System32") if os.name == "nt" else str(empty),
                       PYTHONPATH=str(ROOT), LIFE_OS_WORKER_LANE="heavy")
    for key in ("LIFE_OS_RECOVERY_SESSION", "LIFE_OS_WORKER_START_GATE"):
        environment.pop(key, None)
    code, out, _ = run_bounded([sys.executable, "-c",
        "import json; from life_os.ai_cli import command; "
        "print(json.dumps({p:command(p) is None for p in ('codex','claude','opencode')}))"],
        cwd=ROOT, env=environment, timeout=15)
    absent = json.loads(out) if code == 0 else {}
    if absent != {"codex": True, "claude": True, "opencode": True}:
        raise RuntimeError("Could not establish model command absence in the disposable worker environment")
    database = home / "life.db"
    c = connect(database)
    try:
        initialize(c)
        worker = [sys.executable, "-m", "life_os", "--db", str(database), "worker", "--once",
                  "--home", str(home), "--backups", str(home / "backups"), "--log", str(home / "worker.log")]
        baseline = fabric.submit(c, "task: Preserve the pre-fault native receipt")
        code, _out, _err = run_bounded(worker, cwd=ROOT, env=environment, timeout=60)
        if code or c.execute("SELECT state FROM execution_requests WHERE id=?", (baseline,)).fetchone()[0] != "succeeded":
            raise RuntimeError("Actual worker did not establish the pre-fault completed receipt")
        completed_before = _receipts(c)
        recipes.repair(c, recipes.ARTIFACT_CAPABILITY, home=home)
        capability = c.execute("SELECT metadata_json FROM capabilities WHERE name=?", (recipes.ARTIFACT_CAPABILITY,)).fetchone()
        metadata = json.loads(capability[0])
        metadata["circuit_breaker"] = {"state": "open", "consecutive_failures": 1}
        set_capability_health(c, recipes.ARTIFACT_CAPABILITY, "unavailable", metadata)
        rid = fabric.submit(c, "Disposable persisted plan: retain prefix and restore local artifact route")
        plan = fabric.validate_plan({"summary": "Explicit pre-fault fixture plan", "steps": [
            {"operation": "task.add", "payload": "Create this original-request prefix once"},
            {"operation": "artifact.write", "payload": json.dumps({"filename": "proof.json", "content": '{"proof":true}'})},
        ]})
        # Fixture seeding is intentionally confined to the new disposable DB.
        # The real worker consumes and resumes this exact durable original plan.
        c.execute("UPDATE execution_requests SET plan_json=?,provider='explicit-fixture' WHERE id=?", (json.dumps(plan), rid))
        for ordinal, step in enumerate(plan["steps"]):
            c.execute("INSERT INTO execution_steps(request_id,ordinal,operation,payload) VALUES(?,?,?,?)",
                      (rid, ordinal, step["operation"], step["payload"]))
        c.commit()
        started = time.monotonic()
        code, _out, _err = run_bounded(worker, cwd=ROOT, env=environment, timeout=60)
        elapsed = time.monotonic() - started
        request = dict(c.execute("SELECT * FROM execution_requests WHERE id=?", (rid,)).fetchone())
        gap = dict(c.execute("SELECT * FROM capability_build_tasks WHERE request_id=?", (rid,)).fetchone())
        completed_after = _receipts(c)
        retained = all(receipt in completed_after for receipt in completed_before)
        prefix_count = c.execute("SELECT COUNT(*) FROM tasks WHERE title='Create this original-request prefix once'").fetchone()[0]
        jobs = [dict(row) for row in c.execute("SELECT kind,state,result_json FROM worker_jobs ORDER BY id")]
        if (code or request["state"] != "succeeded" or gap["state"] != "resolved"
                or request["generation"] != 1 or not retained or prefix_count != 1
                or c.execute("SELECT COUNT(*) FROM engineering_runs").fetchone()[0] != 0):
            raise RuntimeError("Disposable native worker proof did not satisfy same-request recovery")
        result = {"passed": True, "scope": "disposable actual worker CLI; canonical state untouched",
                  "model_commands_absent": absent, "request_id": rid, "original_plan_retained": request["plan_json"] == json.dumps(plan),
                  "resume_generation": request["generation"], "gap_id": gap["id"], "gap_state": gap["state"],
                  "pre_fault_receipts": len(completed_before), "pre_fault_receipts_digest": _digest(completed_before),
                  "pre_fault_receipts_retained": retained, "prefix_count": prefix_count,
                  "recovery_seconds": round(elapsed, 3), "worker_jobs": jobs,
                  "model_inference": False, "external_actions": False, "candidate_code_executed": False}
        (home / "proof.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        c.close()


if __name__ == "__main__":
    main()
