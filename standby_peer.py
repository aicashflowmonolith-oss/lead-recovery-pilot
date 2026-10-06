from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import math
import os
import time

TOKEN_ENV = "MONOLITH_PEER_TOKEN"
PEER_ID_ENV = "MONOLITH_PEER_ID"
BODY_LIMIT = 32768
CAPABILITIES = [
    "operation.calculate_roi",
    "operation.measure_workflow",
    "operation.policy_review",
]
POLICY_ACTIONS = {
    "LOCAL_REVIEW_QUEUE": ("ALLOW", "local deterministic work is allowed"),
    "EXTERNAL_COMMUNICATION": ("DENY", "Batch 1 prohibits external or state-changing actions"),
    "CUSTOMER_CONTACT": ("DENY", "Batch 1 prohibits external or state-changing actions"),
}

def _peer_id():
    value = os.environ.get(PEER_ID_ENV, "render-free-1").strip()
    return value if value and len(value) <= 96 else "invalid-peer-id"

def _token():
    value = os.environ.get(TOKEN_ENV, "").strip()
    return value if 32 <= len(value) <= 512 else None

def _number(name, value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name)
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise ValueError(name)
    return number

def _execute(operation, data):
    if not isinstance(data, dict):
        raise ValueError("data")
    if operation == "calculate_roi":
        if set(data) != {"net_profit", "investment", "source_reference"}:
            raise ValueError("fields")
        profit = data["net_profit"]
        if isinstance(profit, bool) or not isinstance(profit, (int, float)) or not math.isfinite(float(profit)):
            raise ValueError("net_profit")
        investment = _number("investment", data["investment"], positive=True)
        ref = data["source_reference"]
        if not isinstance(ref, str) or not 1 <= len(ref) <= 200:
            raise ValueError("source_reference")
        return {
            "roi_percent": float(profit) / investment * 100,
            "classification": "DERIVED_FROM_UNVERIFIED_INPUTS",
            "investment": investment,
            "net_profit": profit,
            "source_reference": ref,
            "revenue_verified": False,
            "money_moved": False,
        }
    if operation == "measure_workflow":
        if set(data) != {"before_minutes", "after_minutes", "review_minutes", "source_reference"}:
            raise ValueError("fields")
        before = _number("before_minutes", data["before_minutes"])
        after = _number("after_minutes", data["after_minutes"])
        review = _number("review_minutes", data["review_minutes"])
        ref = data["source_reference"]
        if not isinstance(ref, str) or not 1 <= len(ref) <= 200:
            raise ValueError("source_reference")
        return {
            "time_saved_minutes": before - after - review,
            "source_reference": ref,
            "economic_value": None,
            "classification": "DERIVED_FROM_UNVERIFIED_INPUTS",
            "automation_delivered": False,
        }
    if operation == "policy_review":
        if set(data) != {"action"} or data["action"] not in POLICY_ACTIONS:
            raise ValueError("action")
        decision, reason = POLICY_ACTIONS[data["action"]]
        return {
            "action": data["action"],
            "policy_decision": decision,
            "reason": reason,
            "execution_authorized": False,
        }
    raise PermissionError("operation")

def _evidence(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()

def _write(handler, status, body):
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(payload)

def _authorized(handler):
    token = _token()
    supplied = handler.headers.get("Authorization", "")
    return bool(token and supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:], token))

def _read(handler):
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        raise ValueError("length")
    length = int(raw_length)
    if not 0 <= length <= BODY_LIMIT:
        raise ValueError("length")
    value = json.loads(handler.rfile.read(length).decode())
    if not isinstance(value, dict):
        raise ValueError("json")
    return value

class Handler(BaseHTTPRequestHandler):
    server_version = "MONOLITHStandbyPeer/1"
    sys_version = ""
    def log_message(self, *_):
        return
    def do_GET(self):
        if self.path != "/health":
            _write(self, 404, {"error": "not_found"})
            return
        configured = _token() is not None
        _write(self, 200 if configured else 503, {
            "service": "monolith-execution-peer",
            "provider_id": _peer_id(),
            "healthy": configured,
            "capabilities": CAPABILITIES,
        })
    def do_POST(self):
        if self.path not in ("/probe", "/execute"):
            _write(self, 404, {"error": "not_found"})
            return
        if _token() is None:
            _write(self, 503, {"error": "peer_not_configured"})
            return
        if not _authorized(self):
            _write(self, 401, {"error": "unauthorized"})
            return
        try:
            body = _read(self)
        except Exception:
            _write(self, 400, {"error": "invalid_request"})
            return
        if self.path == "/probe":
            if set(body) != {"probe", "provider_id"} or body.get("probe") != "monolith" or body.get("provider_id") != _peer_id():
                _write(self, 400, {"error": "invalid_probe"})
                return
            _write(self, 200, {"ok": True, "provider_id": _peer_id(), "capabilities": CAPABILITIES})
            return
        if set(body) != {"request_id", "operation", "data"}:
            _write(self, 400, {"error": "invalid_execute_envelope"})
            return
        request_id, operation, data = body.get("request_id"), body.get("operation"), body.get("data")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or not isinstance(operation, str) or not isinstance(data, dict):
            _write(self, 400, {"error": "invalid_execute_request"})
            return
        started = time.process_time()
        try:
            result = _execute(operation, data)
        except PermissionError:
            _write(self, 403, {"error": "operation_unavailable"})
            return
        except Exception:
            _write(self, 400, {"error": "invalid_operation_input"})
            return
        evidence = {"request_id": request_id, "operation": operation, "result": result, "peer_id": _peer_id()}
        _write(self, 200, {
            "ok": True,
            "request_id": request_id,
            "operation": operation,
            "result": result,
            "process_seconds": time.process_time() - started,
            "evidence_sha256": _evidence(evidence),
            "peer_id": _peer_id(),
        })

def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "10000"))
    if not 1 <= port <= 65535:
        raise SystemExit("invalid PORT")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
