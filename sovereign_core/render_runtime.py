import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import monolith as core
import autonomy

DB_PATH = os.environ.get("MONOLITH_DB", "/tmp/sovereign-core.db")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
CONTROL_TOKEN = os.environ.get("MONOLITH_CONTROL_TOKEN", "")
STOP = threading.Event()


def valid_control_token(token: str) -> bool:
    return len(token) >= 32


def authorized_header(header: str | None, token: str) -> bool:
    if not valid_control_token(token) or not header:
        return False
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    supplied = header[len(prefix):]
    return hmac.compare_digest(supplied, token)


def worker_loop() -> None:
    core.init_db(DB_PATH)
    conn = core.connect(DB_PATH)
    autonomy.init_autonomy(conn)
    try:
        while not STOP.is_set():
            auto = autonomy.tick(conn)
            worked = core.work_once(conn)
            if (
                worked is None
                and auto["schedules_submitted"] == 0
                and auto["goals"]["submitted"] == 0
                and auto["goals"]["completed"] == 0
                and auto["goals"]["blocked"] == 0
            ):
                STOP.wait(1.0)
    finally:
        conn.close()


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "SovereignCore/0.2"

    def send_json(self, status: int, body) -> None:
        raw = core.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def require_auth(self) -> bool:
        if authorized_header(self.headers.get("Authorization"), CONTROL_TOKEN):
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("invalid body size")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def do_GET(self) -> None:
        if self.path == "/health":
            conn = core.connect(DB_PATH)
            try:
                status = core.get_status(conn)
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "worker": True,
                        "control_auth": valid_control_token(CONTROL_TOKEN),
                        "schema_version": status["schema_version"],
                        "adapters": len(status["adapters"]),
                        "tasks": status["tasks"],
                        "autonomy": autonomy.status(conn),
                    },
                )
            finally:
                conn.close()
            return

        if not self.require_auth():
            return

        conn = core.connect(DB_PATH)
        try:
            if self.path == "/status":
                self.send_json(200, core.get_status(conn))
            elif self.path == "/goals":
                self.send_json(200, {"goals": autonomy.list_goals(conn, 100)})
            elif self.path == "/schedules":
                self.send_json(200, {"schedules": autonomy.list_schedules(conn, 100)})
            elif self.path.startswith("/tasks"):
                self.send_json(200, {"tasks": core.list_tasks(conn, 100)})
            else:
                self.send_json(404, {"error": "not found"})
        except Exception as exc:
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    def do_POST(self) -> None:
        if not self.require_auth():
            return

        conn = core.connect(DB_PATH)
        try:
            body = self.read_json()
            if self.path == "/command":
                self.send_json(200, core.command(conn, str(body.get("text", ""))))
                return
            if self.path == "/goals":
                steps = body.get("steps", [])
                if not isinstance(steps, list):
                    raise ValueError("steps must be an array")
                goal = autonomy.create_goal(
                    conn,
                    str(body.get("title", "")),
                    str(body.get("objective", "")),
                    steps,
                    priority=int(body.get("priority", 0)),
                )
                self.send_json(201, goal)
                return
            if self.path == "/schedules":
                payload = body.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                schedule = autonomy.add_schedule(
                    conn,
                    str(body.get("name", "")),
                    str(body.get("kind", "")),
                    payload,
                    interval_seconds=float(body.get("interval_seconds", 0)),
                    priority=int(body.get("priority", 0)),
                    next_run=body.get("next_run"),
                )
                self.send_json(201, schedule)
                return
            if self.path == "/tasks":
                kind = str(body.get("kind", ""))
                payload = body.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                task = core.submit_task(
                    conn,
                    kind,
                    payload,
                    priority=int(body.get("priority", 0)),
                    max_attempts=int(body.get("max_attempts", 3)),
                    actor="remote-control",
                )
                self.send_json(201, task)
                return
            if self.path.startswith("/tasks/") and self.path.endswith("/approve"):
                task_id = int(self.path.split("/")[2])
                self.send_json(200, core.approve_task(conn, task_id, actor="remote-control"))
                return
            if self.path.startswith("/tasks/") and self.path.endswith("/reject"):
                task_id = int(self.path.split("/")[2])
                self.send_json(
                    200,
                    core.reject_task(
                        conn,
                        task_id,
                        str(body.get("reason", "rejected")),
                        actor="remote-control",
                    ),
                )
                return
            self.send_json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    if not valid_control_token(CONTROL_TOKEN):
        raise SystemExit("MONOLITH_CONTROL_TOKEN must be set to at least 32 characters")
    core.init_db(DB_PATH)
    init_conn = core.connect(DB_PATH)
    try:
        autonomy.init_autonomy(init_conn)
    finally:
        init_conn.close()
    thread = threading.Thread(target=worker_loop, name="sovereign-worker", daemon=True)
    thread.start()
    server = HTTPServer((HOST, PORT), ControlHandler)
    try:
        server.serve_forever()
    finally:
        STOP.set()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
