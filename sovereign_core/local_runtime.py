import os
import secrets
import threading
from http.server import HTTPServer
from pathlib import Path

import monolith as core
import render_runtime as web

INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SovereignCore"
STATE_DIR = Path(os.environ.get("MONOLITH_STATE_DIR", str(INSTALL_DIR / "state")))
DB_PATH = str(STATE_DIR / "monolith.db")
TOKEN_FILE = STATE_DIR / "control.token"
HOST = "127.0.0.1"
PORT = int(os.environ.get("MONOLITH_PORT", "8765"))


def ensure_control_token() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if web.valid_control_token(token):
            return token
    token = secrets.token_urlsafe(48)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


def main() -> None:
    token = ensure_control_token()
    core.init_db(DB_PATH)

    web.DB_PATH = DB_PATH
    web.CONTROL_TOKEN = token
    web.STOP.clear()

    worker = threading.Thread(target=web.worker_loop, name="sovereign-worker", daemon=True)
    worker.start()
    server = HTTPServer((HOST, PORT), web.ControlHandler)
    try:
        server.serve_forever()
    finally:
        web.STOP.set()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
