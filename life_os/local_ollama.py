"""Zero-cost local engineering provider used as MONOLITH's provider floor.

The adapter talks only to a loopback Ollama service and exposes a deliberately
small file-operation protocol inside an already-isolated engineering worktree.
The model never receives shell, Git, network, credential, or host authority;
trusted host verification remains responsible for tests and commits.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .ai_cli import AdapterError, CapabilityUnavailable

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:1.5b"
LOW_RESOURCE_MODEL = "qwen2.5:0.5b"
LOW_RESOURCE_MAX_TOTAL_MB = 6144
CHAT_TURN_TIMEOUT_SECONDS = 60
MAX_WALL_SECONDS = 120
MAX_TURNS = 12
MAX_FILE_BYTES = 256 * 1024
MAX_RESULT_CHARS = 24_000
PROTECTED_NAMES = {
    ".env", "life.db", "life.db-wal", "life.db-shm", "auth.json",
    "credentials.json", "credentials", "secrets.json", "secrets",
}
PROTECTED_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".keystore"}


def _total_memory_mb() -> int | None:
    """Best-effort physical-memory size without adding a dependency."""
    if os.name == "nt":
        try:
            import ctypes
            class Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (key, ctypes.c_ulonglong) for key in (
                        "total_phys", "avail_phys", "total_page", "avail_page",
                        "total_virtual", "avail_virtual", "extended",
                    )
                ]
            value = Memory()
            value.length = ctypes.sizeof(value)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                return max(1, int(value.total_phys // (1024 * 1024)))
        except (AttributeError, OSError):
            return None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return max(1, int((pages * page_size) // (1024 * 1024)))
    except (AttributeError, OSError, ValueError):
        return None


def model_candidates() -> tuple[str, ...]:
    override = os.environ.get("LIFE_OS_OLLAMA_ENGINEERING_MODEL", "").strip()
    if override:
        return (override,)
    total = _total_memory_mb()
    if total is not None and total <= LOW_RESOURCE_MAX_TOTAL_MB:
        return (LOW_RESOURCE_MODEL, DEFAULT_MODEL)
    return (DEFAULT_MODEL, LOW_RESOURCE_MODEL)


def configured_model() -> str:
    return model_candidates()[0]


def _request_json(path: str, payload: dict[str, Any] | None = None, *, timeout: int = 5) -> dict[str, Any]:
    if not path.startswith("/"):
        raise ValueError("Ollama API path must be absolute")
    url = OLLAMA_BASE_URL + path
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024 + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise CapabilityUnavailable("Local Ollama service is unavailable") from exc
    if len(body) > 4 * 1024 * 1024:
        raise AdapterError("Local Ollama response exceeded 4 MiB")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("Local Ollama returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise AdapterError("Local Ollama returned a non-object response")
    return value


def installed_models() -> tuple[str, ...]:
    response = _request_json("/api/tags", timeout=15)
    models = response.get("models")
    if not isinstance(models, list):
        return ()
    result = []
    for item in models:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            result.append(item["name"])
    return tuple(result)


def _ollama_command() -> str | None:
    command = shutil.which("ollama") or shutil.which("ollama.exe")
    if command:
        return command
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _models_with_recovery(*, allow_start: bool) -> tuple[str, ...]:
    try:
        # A responsive server with no matching model is not a service failure.
        # Do not spawn a competing server in that case.
        return installed_models()
    except CapabilityUnavailable:
        if not allow_start:
            return ()
    command = _ollama_command()
    if not command:
        return ()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        subprocess.Popen(
            [command, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    except OSError:
        return ()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            return installed_models()
        except CapabilityUnavailable:
            continue
    return ()


def ready_model(*, allow_start: bool = True) -> str | None:
    installed = set(_models_with_recovery(allow_start=allow_start))
    return next((model for model in model_candidates() if model in installed), None)


def ensure_ready(*, model: str | None = None, allow_start: bool = True) -> bool:
    installed = set(_models_with_recovery(allow_start=allow_start))
    if model is not None:
        return model in installed
    return any(candidate in installed for candidate in model_candidates())

def _safe_relative(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("a relative path is required")
    path = Path(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path escapes the engineering worktree")
    parts = [part.lower() for part in path.parts]
    name = parts[-1]
    suffix = Path(name).suffix.lower()
    protected_part = any(
        part == ".git" or part in PROTECTED_NAMES or part.startswith(".env")
        or part.startswith("credentials.") or part.startswith("secrets.")
        for part in parts
    )
    if protected_part or suffix in PROTECTED_SUFFIXES:
        raise ValueError("protected path is not available to the local provider")
    return path


def _resolve(root: Path, raw: Any, *, for_write: bool = False) -> Path:
    relative = _safe_relative(raw)
    target = (root / relative).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the engineering worktree") from exc
    cursor = root
    for part in relative.parts[:-1] if for_write else relative.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("symlink traversal is not allowed")
    if target.exists() and target.is_symlink():
        raise ValueError("symlink traversal is not allowed")
    return target


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError("file does not exist")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file is too large for the local provider")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("binary files are not available to the local provider") from exc


def _result(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), sort_keys=True)
    return text[:MAX_RESULT_CHARS]


def _execute_action(root: Path, action: dict[str, Any]) -> tuple[str, bool]:
    name = action.get("action")
    if name == "done":
        return _result({"ok": True, "done": True, "summary": str(action.get("summary", ""))[:1000]}), True
    if name == "list":
        relative = action.get("path", ".")
        directory = root if relative in ("", ".", None) else _resolve(root, relative)
        if not directory.is_dir():
            raise ValueError("directory does not exist")
        entries = []
        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            try:
                rel = item.relative_to(root).as_posix()
                _safe_relative(rel)
            except ValueError:
                continue
            entries.append({"path": rel, "type": "dir" if item.is_dir() else "file"})
            if len(entries) >= 200:
                break
        return _result(entries), False
    if name == "read":
        path = _resolve(root, action.get("path"))
        return _result(_read_text(path)), False
    if name == "search":
        needle = action.get("text")
        if not isinstance(needle, str) or not needle or len(needle) > 300:
            raise ValueError("search text must be 1..300 characters")
        relative = action.get("path", ".")
        base = root if relative in ("", ".", None) else _resolve(root, relative)
        candidates = [base] if base.is_file() else base.rglob("*")
        matches = []
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            try:
                rel = path.relative_to(root).as_posix()
                _safe_relative(rel)
                text = _read_text(path)
            except (ValueError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if needle in line:
                    matches.append({"path": rel, "line": line_no, "text": line[:500]})
                    if len(matches) >= 100:
                        return _result(matches), False
        return _result(matches), False
    if name == "write":
        path = _resolve(root, action.get("path"), for_write=True)
        content = action.get("content")
        if not isinstance(content, str):
            raise ValueError("write content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError("write exceeds local-provider file limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".monolith-tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
        return _result({"ok": True, "path": path.relative_to(root).as_posix(), "bytes": len(encoded)}), False
    if name == "replace":
        path = _resolve(root, action.get("path"), for_write=True)
        old = action.get("old")
        new = action.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("replace requires non-empty old text and replacement text")
        text = _read_text(path)
        count = text.count(old)
        if count != 1:
            raise ValueError(f"replace requires exactly one match; found {count}")
        updated = text.replace(old, new, 1)
        if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("replace exceeds local-provider file limit")
        temporary = path.with_name(path.name + ".monolith-tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, path)
        return _result({"ok": True, "path": path.relative_to(root).as_posix()}), False
    raise ValueError("unsupported local-provider action")


_CHAT_HELPER = r"""
import json
import sys
import urllib.request
payload = json.load(sys.stdin)
request = urllib.request.Request(
    'http://127.0.0.1:11434/api/chat',
    data=json.dumps(payload, separators=(',', ':')).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(request, timeout=120) as response:
    body = response.read(4 * 1024 * 1024 + 1)
if len(body) > 4 * 1024 * 1024:
    raise RuntimeError('response too large')
sys.stdout.write(body.decode('utf-8'))
"""


def _chat_request(payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", _CHAT_HELPER],
            input=json.dumps(payload, separators=(",", ":")),
            capture_output=True, text=True, timeout=max(1, int(timeout)),
            creationflags=creationflags, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapabilityUnavailable("Local Ollama provider exceeded hard turn deadline") from exc
    if result.returncode != 0:
        raise CapabilityUnavailable("Local Ollama provider request failed")
    if len(result.stdout.encode("utf-8", errors="ignore")) > 4 * 1024 * 1024:
        raise AdapterError("Local Ollama response exceeded 4 MiB")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError("Local Ollama returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise AdapterError("Local Ollama returned a non-object response")
    return value


def _chat(model: str, messages: list[dict[str, str]], *, timeout: int = CHAT_TURN_TIMEOUT_SECONDS) -> dict[str, Any]:
    response = _chat_request({
        "model": model, "messages": messages, "stream": False, "format": "json",
        "keep_alive": "2m", "options": {"temperature": 0.1, "num_predict": 128, "num_ctx": 2048},
    }, timeout=timeout)
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise CapabilityUnavailable("Local Ollama provider returned no action")
    try:
        action = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CapabilityUnavailable("Local Ollama provider returned invalid action JSON") from exc
    if not isinstance(action, dict):
        raise CapabilityUnavailable("Local Ollama provider action must be an object")
    return action


def _unload(model: str) -> None:
    try:
        _request_json("/api/generate", {"model": model, "keep_alive": 0}, timeout=10)
    except (AdapterError, CapabilityUnavailable):
        pass


def invoke(
    worktree: Path, prompt: str, *, model: str | None = None,
    pulse: Callable[[], None] | None = None,
) -> None:
    """Run one bounded local engineering turn inside *worktree*.

    The model can only request operations implemented by ``_execute_action``.
    Independent Git/test verification remains outside this trust boundary.
    """
    root = Path(worktree).resolve()
    if not root.is_dir():
        raise ValueError("engineering worktree does not exist")
    selected = model or ready_model(allow_start=True)
    if not selected or not ensure_ready(model=selected):
        raise CapabilityUnavailable(f"Local Ollama model is unavailable: {selected or 'none'}")
    system = (
        "You are MONOLITH's local emergency engineering provider. You have no shell, Git, network, "
        "credential, or host access. Work only through one JSON action per turn. Allowed actions: "
        "list {path?}; read {path}; search {path?,text}; write {path,content}; "
        "replace {path,old,new}; done {summary}. Paths are relative to the isolated worktree. "
        "Inspect before editing. Make the smallest correct change. Never request protected data. "
        "The trusted host will run tests and Git verification after you finish. Output JSON only."
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    deadline = time.monotonic() + MAX_WALL_SECONDS
    try:
        for _ in range(MAX_TURNS):
            if pulse is not None:
                pulse()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CapabilityUnavailable("Local Ollama provider exceeded hard wall-clock limit")
            action = _chat(selected, messages, timeout=min(CHAT_TURN_TIMEOUT_SECONDS, max(1, int(remaining))))
            try:
                result, done = _execute_action(root, action)
            except (ValueError, OSError) as exc:
                result, done = _result({"ok": False, "error": str(exc)[:1000]}), False
            messages.append({"role": "assistant", "content": json.dumps(action, separators=(",", ":"))})
            messages.append({"role": "user", "content": "HOST_RESULT " + result})
            if done:
                return
        raise CapabilityUnavailable("Local Ollama provider exceeded bounded action limit")
    finally:
        _unload(selected)
