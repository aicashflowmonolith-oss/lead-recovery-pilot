from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

DEFAULT_DB = os.environ.get("MONOLITH_DB", str(Path("state") / "monolith.db"))
AUTO_APPROVE_RISKS = {"read", "low"}
RISK_ORDER = {"read": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def now() -> float:
    return time.time()


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS adapters (
    name TEXT PRIMARY KEY,
    risk TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL,
    verification_required INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    not_before REAL NOT NULL DEFAULT 0,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'not_required',
    result TEXT,
    last_error TEXT,
    verification TEXT,
    FOREIGN KEY(kind) REFERENCES adapters(name)
);

CREATE INDEX IF NOT EXISTS idx_tasks_runnable
ON tasks(status, not_before, priority, id);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL UNIQUE,
    requested_at REAL NOT NULL,
    decided_at REAL,
    state TEXT NOT NULL,
    reason TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts, id);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT,
    data TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts, id);
"""


def audit(conn: sqlite3.Connection, actor: str, action: str, object_type: str,
          object_id: str | int | None, data: Any) -> None:
    conn.execute(
        "INSERT INTO audit(ts,actor,action,object_type,object_id,data) VALUES(?,?,?,?,?,?)",
        (now(), actor, action, object_type, None if object_id is None else str(object_id), dumps(data)),
    )


def emit(conn: sqlite3.Connection, event_type: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO events(ts,type,payload) VALUES(?,?,?)",
        (now(), event_type, dumps(payload)),
    )


def execute_echo(conn: sqlite3.Connection, payload: dict[str, Any]) -> Any:
    return payload


def verify_echo(conn: sqlite3.Connection, payload: dict[str, Any], result: Any) -> bool:
    return result == payload


def execute_state_set(conn: sqlite3.Connection, payload: dict[str, Any]) -> Any:
    if "key" not in payload or "value" not in payload:
        raise ValueError("state.set requires key and value")
    key = str(payload["key"]).strip()
    if not key:
        raise ValueError("state key cannot be empty")
    value = dumps(payload["value"])
    source = str(payload.get("source", "operator"))
    confidence = float(payload.get("confidence", 1.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    conn.execute(
        """INSERT INTO state(key,value,source,confidence,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             value=excluded.value,
             source=excluded.source,
             confidence=excluded.confidence,
             updated_at=excluded.updated_at""",
        (key, value, source, confidence, now()),
    )
    return {"key": key, "value": payload["value"], "source": source, "confidence": confidence}


def verify_state_set(conn: sqlite3.Connection, payload: dict[str, Any], result: Any) -> bool:
    row = conn.execute("SELECT value FROM state WHERE key=?", (str(payload["key"]).strip(),)).fetchone()
    return row is not None and loads(row["value"]) == payload["value"]


def execute_state_get(conn: sqlite3.Connection, payload: dict[str, Any]) -> Any:
    if "key" not in payload:
        raise ValueError("state.get requires key")
    key = str(payload["key"]).strip()
    row = conn.execute(
        "SELECT key,value,source,confidence,updated_at FROM state WHERE key=?",
        (key,),
    ).fetchone()
    if row is None:
        return {"key": key, "found": False}
    return {
        "key": row["key"],
        "found": True,
        "value": loads(row["value"]),
        "source": row["source"],
        "confidence": row["confidence"],
        "updated_at": row["updated_at"],
    }


def verify_state_get(conn: sqlite3.Connection, payload: dict[str, Any], result: Any) -> bool:
    return isinstance(result, dict) and result.get("key") == str(payload["key"]).strip()


Executor = Callable[[sqlite3.Connection, dict[str, Any]], Any]
Verifier = Callable[[sqlite3.Connection, dict[str, Any], Any], bool]

BUILTINS: dict[str, tuple[str, str, Executor, Verifier]] = {
    "echo": ("read", "Return a payload unchanged.", execute_echo, verify_echo),
    "state.set": ("low", "Write a provenance-bearing canonical state value.", execute_state_set, verify_state_set),
    "state.get": ("read", "Read a canonical state value.", execute_state_get, verify_state_get),
}


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        ts = now()
        for name, (risk, description, _, _) in BUILTINS.items():
            conn.execute(
                """INSERT INTO adapters(name,risk,enabled,description,verification_required,updated_at)
                   VALUES(?,?,?,?,1,?)
                   ON CONFLICT(name) DO UPDATE SET
                     risk=excluded.risk,
                     description=excluded.description,
                     updated_at=excluded.updated_at""",
                (name, risk, 1, description, ts),
            )
        conn.execute(
            """INSERT INTO meta(key,value,updated_at) VALUES('schema_version','1',?)
               ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at""",
            (ts,),
        )
        audit(conn, "system", "initialize", "database", db_path, {"schema_version": 1})
        emit(conn, "system.initialized", {"schema_version": 1})
    finally:
        conn.close()


def row_to_task(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = loads(item["payload"], {})
    item["result"] = loads(item["result"], None)
    item["verification"] = loads(item["verification"], None)
    item["requires_approval"] = bool(item["requires_approval"])
    return item


def get_adapter(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT name,risk,enabled,description,verification_required FROM adapters WHERE name=?",
        (kind,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown adapter: {kind}")
    if not row["enabled"]:
        raise ValueError(f"adapter disabled: {kind}")
    if kind not in BUILTINS:
        raise ValueError(f"adapter has no installed executor: {kind}")
    return row


def submit_task(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any],
    priority: int = 0,
    max_attempts: int = 3,
    actor: str = "operator",
) -> dict[str, Any]:
    adapter = get_adapter(conn, kind)
    if max_attempts < 1 or max_attempts > 20:
        raise ValueError("max_attempts must be between 1 and 20")
    risk = adapter["risk"]
    if risk not in RISK_ORDER:
        raise ValueError(f"invalid adapter risk: {risk}")
    requires_approval = risk not in AUTO_APPROVE_RISKS
    status = "waiting_approval" if requires_approval else "pending"
    approval_state = "pending" if requires_approval else "not_required"
    ts = now()
    cur = conn.execute(
        """INSERT INTO tasks(
             created_at,updated_at,kind,payload,status,priority,attempts,max_attempts,
             not_before,requires_approval,approval_state
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts, ts, kind, dumps(payload), status, int(priority), 0, int(max_attempts),
            0.0, int(requires_approval), approval_state,
        ),
    )
    task_id = int(cur.lastrowid)
    if requires_approval:
        conn.execute(
            "INSERT INTO approvals(task_id,requested_at,state) VALUES(?,?,'pending')",
            (task_id, ts),
        )
    audit(conn, actor, "submit", "task", task_id, {"kind": kind, "risk": risk, "payload": payload})
    emit(conn, "task.submitted", {"task_id": task_id, "kind": kind, "status": status})
    return row_to_task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())  # type: ignore[return-value]


def approve_task(conn: sqlite3.Connection, task_id: int, actor: str = "operator") -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    if not row["requires_approval"]:
        raise ValueError("task does not require approval")
    if row["approval_state"] != "pending":
        raise ValueError(f"approval already decided: {row['approval_state']}")
    ts = now()
    conn.execute(
        "UPDATE approvals SET state='approved',decided_at=? WHERE task_id=?",
        (ts, task_id),
    )
    conn.execute(
        "UPDATE tasks SET approval_state='approved',status='pending',updated_at=? WHERE id=?",
        (ts, task_id),
    )
    audit(conn, actor, "approve", "task", task_id, {})
    emit(conn, "task.approved", {"task_id": task_id})
    return row_to_task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())  # type: ignore[return-value]


def reject_task(conn: sqlite3.Connection, task_id: int, reason: str, actor: str = "operator") -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    if not row["requires_approval"]:
        raise ValueError("task does not require approval")
    if row["approval_state"] != "pending":
        raise ValueError(f"approval already decided: {row['approval_state']}")
    ts = now()
    conn.execute(
        "UPDATE approvals SET state='rejected',decided_at=?,reason=? WHERE task_id=?",
        (ts, reason, task_id),
    )
    conn.execute(
        "UPDATE tasks SET approval_state='rejected',status='rejected',updated_at=?,last_error=? WHERE id=?",
        (ts, reason, task_id),
    )
    audit(conn, actor, "reject", "task", task_id, {"reason": reason})
    emit(conn, "task.rejected", {"task_id": task_id, "reason": reason})
    return row_to_task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())  # type: ignore[return-value]


def recover_stale(conn: sqlite3.Connection, stale_seconds: float = 300.0) -> int:
    cutoff = now() - stale_seconds
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status='running' AND updated_at<?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """UPDATE tasks SET status='pending',updated_at=?,not_before=?,
               last_error='recovered stale running task' WHERE id=?""",
            (now(), now(), row["id"]),
        )
        audit(conn, "system", "recover_stale", "task", row["id"], {})
        emit(conn, "task.recovered", {"task_id": row["id"]})
    return len(rows)


def claim_task(conn: sqlite3.Connection) -> dict[str, Any] | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT * FROM tasks
               WHERE status='pending'
                 AND not_before<=?
                 AND (requires_approval=0 OR approval_state='approved')
               ORDER BY priority DESC,id ASC
               LIMIT 1""",
            (now(),),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        ts = now()
        conn.execute(
            "UPDATE tasks SET status='running',updated_at=?,attempts=attempts+1 WHERE id=?",
            (ts, row["id"]),
        )
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    task_obj = row_to_task(task)
    if task_obj is not None:
        audit(conn, "worker", "claim", "task", task_obj["id"], {"attempt": task_obj["attempts"]})
        emit(conn, "task.started", {"task_id": task_obj["id"], "attempt": task_obj["attempts"]})
    return task_obj


def run_task(conn: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any]:
    task_id = int(task["id"])
    kind = str(task["kind"])
    payload = task["payload"]
    _, _, executor, verifier = BUILTINS[kind]
    try:
        result = executor(conn, payload)
        verified = bool(verifier(conn, payload, result))
        verification = {"ok": verified, "checked_at": now()}
        if not verified:
            raise RuntimeError("verification failed")
        ts = now()
        conn.execute(
            """UPDATE tasks SET status='succeeded',updated_at=?,result=?,verification=?,last_error=NULL
               WHERE id=?""",
            (ts, dumps(result), dumps(verification), task_id),
        )
        audit(conn, "worker", "complete", "task", task_id, {"result": result, "verification": verification})
        emit(conn, "task.succeeded", {"task_id": task_id})
    except Exception as exc:
        current = conn.execute(
            "SELECT attempts,max_attempts FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if current is None:
            raise
        attempts = int(current["attempts"])
        max_attempts = int(current["max_attempts"])
        if attempts >= max_attempts:
            status = "failed"
            not_before = 0.0
        else:
            status = "pending"
            not_before = now() + min(300.0, float(2 ** min(attempts, 8)))
        error = f"{type(exc).__name__}: {exc}"
        conn.execute(
            """UPDATE tasks SET status=?,updated_at=?,not_before=?,last_error=?,verification=?
               WHERE id=?""",
            (status, now(), not_before, error, dumps({"ok": False, "checked_at": now()}), task_id),
        )
        audit(
            conn,
            "worker",
            "failure",
            "task",
            task_id,
            {"error": error, "attempts": attempts, "max_attempts": max_attempts, "next_status": status},
        )
        emit(conn, f"task.{status}", {"task_id": task_id, "error": error})
    return row_to_task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())  # type: ignore[return-value]


def work_once(conn: sqlite3.Connection) -> dict[str, Any] | None:
    recover_stale(conn)
    task = claim_task(conn)
    if task is None:
        return None
    return run_task(conn, task)


def get_status(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status,COUNT(*) AS n FROM tasks GROUP BY status")
    }
    return {
        "ok": True,
        "schema_version": loads(
            dumps(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"])
        ),
        "tasks": counts,
        "state_keys": conn.execute("SELECT COUNT(*) FROM state").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "audit_records": conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
        "adapters": [
            dict(row)
            for row in conn.execute(
                "SELECT name,risk,enabled,description,verification_required FROM adapters ORDER BY name"
            )
        ],
    }


def list_tasks(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    return [row_to_task(row) for row in rows]  # type: ignore[list-item]


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def command(conn: sqlite3.Connection, text: str) -> Any:
    parts = shlex.split(text)
    if not parts:
        raise ValueError("empty command")
    verb = parts[0].lower()
    if verb == "status":
        return get_status(conn)
    if verb == "tasks":
        limit = int(parts[1]) if len(parts) > 1 else 20
        return list_tasks(conn, limit)
    if verb == "echo":
        task = submit_task(conn, "echo", {"text": " ".join(parts[1:])})
        return work_once(conn) if task["status"] == "pending" else task
    if verb == "set":
        if len(parts) < 3:
            raise ValueError("usage: set <key> <value>")
        value = parse_value(" ".join(parts[2:]))
        task = submit_task(conn, "state.set", {"key": parts[1], "value": value})
        return work_once(conn) if task["status"] == "pending" else task
    if verb == "get":
        if len(parts) != 2:
            raise ValueError("usage: get <key>")
        task = submit_task(conn, "state.get", {"key": parts[1]})
        return work_once(conn) if task["status"] == "pending" else task
    if verb == "approve":
        if len(parts) != 2:
            raise ValueError("usage: approve <task_id>")
        return approve_task(conn, int(parts[1]))
    if verb == "reject":
        if len(parts) < 2:
            raise ValueError("usage: reject <task_id> [reason]")
        reason = " ".join(parts[2:]) or "rejected by operator"
        return reject_task(conn, int(parts[1]), reason)
    raise ValueError("unknown command; use status, tasks, echo, set, get, approve, or reject")


class Handler(BaseHTTPRequestHandler):
    server_version = "SovereignCore/0.1"

    @property
    def db_path(self) -> str:
        return self.server.db_path  # type: ignore[attr-defined]

    def send_json(self, status: int, body: Any) -> None:
        raw = dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("invalid body size")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def do_GET(self) -> None:
        conn = connect(self.db_path)
        try:
            if self.path == "/health":
                self.send_json(200, {"ok": True})
                return
            if self.path == "/status":
                self.send_json(200, get_status(conn))
                return
            if self.path.startswith("/tasks"):
                self.send_json(200, {"tasks": list_tasks(conn, 100)})
                return
            self.send_json(404, {"error": "not found"})
        except Exception as exc:
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    def do_POST(self) -> None:
        conn = connect(self.db_path)
        try:
            body = self.read_json()
            if self.path == "/command":
                self.send_json(200, command(conn, str(body.get("text", ""))))
                return
            if self.path == "/tasks":
                kind = str(body.get("kind", ""))
                payload = body.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                task = submit_task(
                    conn,
                    kind,
                    payload,
                    priority=int(body.get("priority", 0)),
                    max_attempts=int(body.get("max_attempts", 3)),
                    actor="http",
                )
                self.send_json(201, task)
                return
            if self.path.startswith("/tasks/") and self.path.endswith("/approve"):
                task_id = int(self.path.split("/")[2])
                self.send_json(200, approve_task(conn, task_id, actor="http"))
                return
            if self.path.startswith("/tasks/") and self.path.endswith("/reject"):
                task_id = int(self.path.split("/")[2])
                self.send_json(
                    200,
                    reject_task(conn, task_id, str(body.get("reason", "rejected")), actor="http"),
                )
                return
            self.send_json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(db_path: str, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and os.environ.get("MONOLITH_ALLOW_REMOTE") != "1":
        raise SystemExit(
            "Refusing non-loopback bind. Set MONOLITH_ALLOW_REMOTE=1 only after adding network access controls."
        )
    httpd = HTTPServer((host, port), Handler)
    httpd.db_path = db_path  # type: ignore[attr-defined]
    print(f"Sovereign Core listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monolith", description="Clean-slate sovereign execution core")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("adapters")

    p_submit = sub.add_parser("submit")
    p_submit.add_argument("--kind", required=True)
    p_submit.add_argument("--payload", default="{}")
    p_submit.add_argument("--priority", type=int, default=0)
    p_submit.add_argument("--max-attempts", type=int, default=3)

    p_worker = sub.add_parser("worker")
    p_worker.add_argument("--once", action="store_true")
    p_worker.add_argument("--poll", type=float, default=1.0)

    p_tasks = sub.add_parser("tasks")
    p_tasks.add_argument("--limit", type=int, default=50)

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("task_id", type=int)

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("task_id", type=int)
    p_reject.add_argument("--reason", default="rejected by operator")

    p_command = sub.add_parser("command")
    p_command.add_argument("text")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    init_db(args.db)
    conn = connect(args.db)
    try:
        if args.cmd == "init":
            print(dumps({"ok": True, "db": args.db}))
        elif args.cmd == "status":
            print(json.dumps(get_status(conn), indent=2, sort_keys=True))
        elif args.cmd == "adapters":
            print(json.dumps(get_status(conn)["adapters"], indent=2, sort_keys=True))
        elif args.cmd == "submit":
            payload = json.loads(args.payload)
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            print(
                json.dumps(
                    submit_task(conn, args.kind, payload, args.priority, args.max_attempts),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.cmd == "tasks":
            print(json.dumps(list_tasks(conn, args.limit), indent=2, sort_keys=True))
        elif args.cmd == "approve":
            print(json.dumps(approve_task(conn, args.task_id), indent=2, sort_keys=True))
        elif args.cmd == "reject":
            print(json.dumps(reject_task(conn, args.task_id, args.reason), indent=2, sort_keys=True))
        elif args.cmd == "command":
            print(json.dumps(command(conn, args.text), indent=2, sort_keys=True))
        elif args.cmd == "worker":
            if args.once:
                print(json.dumps(work_once(conn), indent=2, sort_keys=True))
            else:
                try:
                    while True:
                        worked = work_once(conn)
                        if worked is None:
                            time.sleep(max(0.05, args.poll))
                except KeyboardInterrupt:
                    return 0
        elif args.cmd == "serve":
            conn.close()
            serve(args.db, args.host, args.port)
            return 0
        else:
            raise AssertionError(args.cmd)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
