"""Bounded read-only subscription health through the native Codex CLI.

This optional provider probe starts no thread and performs no model turn. It
cannot log in, consume a reset, buy credits or alter an account. MONOLITH's local
recovery and other execution lanes continue when the CLI or network is absent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from .owned_processes import WindowsJob, ProcessContainmentError

PROBE_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 65536


def subscription_environment():
    environment = os.environ.copy()
    # Existing ChatGPT authentication must never fall through to a paid API key.
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("OPENAI_BASE_URL", None)
    return environment


def read_subscription_status(argv, repo: Path, *, pulse=None, timeout=PROBE_TIMEOUT_SECONDS):
    """Return only sanitized health evidence; ambiguity never clears a circuit."""
    child = container = thread = None
    responses = queue.Queue(maxsize=16)
    deadline = time.monotonic() + max(.1, min(timeout, PROBE_TIMEOUT_SECONDS))
    bytes_received = 0

    def reader():
        nonlocal bytes_received
        while True:
            line = child.stdout.readline(MAX_RESPONSE_BYTES + 1)
            if not line:
                return
            bytes_received += len(line)
            if bytes_received > MAX_RESPONSE_BYTES:
                try:
                    responses.put_nowait(None)
                except queue.Full:
                    pass
                return
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("id") in (1, 2, 3):
                try:
                    responses.put_nowait(value)
                except queue.Full:
                    return

    def request(value, identifier=None):
        child.stdin.write((json.dumps(value) + "\n").encode())
        child.stdin.flush()
        if identifier is None:
            return None
        while time.monotonic() < deadline:
            if pulse:
                pulse()
            try:
                response = responses.get(timeout=min(.2, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                if child.poll() is not None:
                    raise ValueError("Native health endpoint exited without a response")
                continue
            if response is None or response.get("id") != identifier or "error" in response:
                raise ValueError("Native health endpoint returned an invalid response")
            if not isinstance(response.get("result"), dict):
                raise ValueError("Native health endpoint returned an invalid result")
            return response["result"]
        raise TimeoutError("Native health endpoint deadline exceeded")

    try:
        if os.name == "nt":
            container = WindowsJob()
        gate = uuid.uuid4().hex
        relay = Path(__file__).with_name("owned_probe_relay.py").resolve()
        child = subprocess.Popen(
            [sys.executable, str(relay), gate, *argv, "-c", 'forced_login_method="chatgpt"', "-c", "mcp_servers={}",
             "app-server", "--stdio"], cwd=repo, env=subscription_environment(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        if container:
            container.assign(child)
        if time.monotonic() >= deadline:
            raise TimeoutError("Native ownership gate deadline exceeded")
        # The relay cannot start Node/the native endpoint before assignment.
        # Read no more than this line in the relay, preserving all later JSON.
        child.stdin.write((gate + "\n").encode("ascii"))
        child.stdin.flush()
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        request({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "monolith_provider_health", "version": "1.0.0"}}}, 1)
        request({"method": "initialized", "params": {}})
        account = request({"method": "account/read", "id": 2,
                           "params": {"refreshToken": False}}, 2).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            return {"state": "unknown", "reason": "subscription_auth_not_established"}
        usage = request({"method": "account/rateLimits/read", "id": 3}, 3)
        ordinary = usage.get("ordinaryUsageAllowed")
        if type(ordinary) is not bool:
            return {"state": "unknown", "reason": "authoritative_usage_unavailable"}
        buckets = usage.get("rateLimitsByLimitId")
        bucket = buckets.get("codex") if isinstance(buckets, dict) else usage.get("rateLimits")
        if not isinstance(bucket, dict) or bucket.get("limitId") != "codex":
            return {"state": "unknown", "reason": "codex_usage_bucket_unavailable"}
        windows = {}
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if window is not None:
                if (not isinstance(window, dict) or type(window.get("usedPercent")) not in (int, float)
                        or not 0 <= window["usedPercent"] <= 100):
                    return {"state": "unknown", "reason": "usage_window_invalid"}
                windows[key] = {field: window.get(field) for field in
                                ("usedPercent", "windowDurationMins", "resetsAt")}
        if not windows:
            return {"state": "unknown", "reason": "usage_windows_unavailable"}
        available = ordinary and not bucket.get("spendControlReached") and not bucket.get("rateLimitReachedType")
        available = available and all(window["usedPercent"] < 100 for window in windows.values())
        # Identity is a provenance hash, never a credential or personal address.
        account_id = usage.get("accountId")
        return {"state": "available" if available else "limited", "ordinary_usage_allowed": ordinary,
                "limit_id": "codex", "execution_health": "quota_available_execution_unverified" if available else "quota_limited",
                "account_identity_sha256": hashlib.sha256(str(account_id).encode()).hexdigest() if account_id else None,
                "windows": windows, "observed_epoch": time.time(), "inference_calls": 0,
                "method": "native_codex_account_rate_limits_read"}
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return {"state": "unknown", "reason": "bounded_native_usage_probe_unavailable"}
    finally:
        contained = True
        containment_failure = None
        if child:
            try:
                child.stdin.close()
            except (OSError, BrokenPipeError):
                pass
        if container:
            try:
                container.terminate(timeout=3)
            except (OSError, TimeoutError):
                contained = False
            except ProcessContainmentError as exc:
                containment_failure = exc
            finally:
                container.close()
        elif child:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                contained = False
        if child:
            try:
                if child.poll() is None and container:
                    # Assignment failure leaves only the gated relay, which
                    # has never been permitted to start the native process.
                    child.kill()
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                contained = False
                child.kill()
                child.wait(timeout=1)
            if thread:
                thread.join(timeout=1)
            if child.stdout and (thread is None or not thread.is_alive()):
                child.stdout.close()
        if containment_failure is not None:
            raise containment_failure
        if not contained:
            return {"state": "unknown", "reason": "native_health_cleanup_unconfirmed"}


def is_quota_failure(circuit):
    text = str(circuit.get("last_error") or "").lower()
    if any(token in text for token in ("unauthenticated", "unauthorized", "authentication",
                                      "sandbox", "tool host", "code-mode", "401")):
        return False
    return any(token in text for token in ("usage limit", "usage-limit", "quota", "try again at"))
