import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import monolith as core

DB_PATH = os.environ.get("MONOLITH_DB", "/tmp/sovereign-core.db")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
STOP = threading.Event()


def worker_loop() -> None:
    core.init_db(DB_PATH)
    conn = core.connect(DB_PATH)
    try:
        while not STOP.is_set():
            worked = core.work_once(conn)
            if worked is None:
                STOP.wait(1.0)
    finally:
        conn.close()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        conn = core.connect(DB_PATH)
        try:
            status = core.get_status(conn)
            body = json.dumps(
                {
                    "ok": True,
                    "worker": True,
                    "schema_version": status["schema_version"],
                    "adapters": len(status["adapters"]),
                    "tasks": status["tasks"],
                },
                separators=(",", ":"),
            ).encode("utf-8")
        finally:
            conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    core.init_db(DB_PATH)
    thread = threading.Thread(target=worker_loop, name="sovereign-worker", daemon=True)
    thread.start()
    server = HTTPServer((HOST, PORT), HealthHandler)
    try:
        server.serve_forever()
    finally:
        STOP.set()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
