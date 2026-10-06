"""Low-overhead host performance and hygiene maintenance."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MEMORY_PRESSURE_PCT = float(os.getenv("LIFE_OS_MEMORY_PRESSURE_PCT", "85"))
DISK_FREE_PRESSURE_PCT = float(os.getenv("LIFE_OS_DISK_FREE_PRESSURE_PCT", "10"))
TEMP_RETENTION_DAYS = int(os.getenv("LIFE_OS_TEMP_RETENTION_DAYS", "7"))
TEMP_DELETE_BUDGET = int(os.getenv("LIFE_OS_TEMP_DELETE_BUDGET", "100"))
PROCESS_ACTION_BUDGET = int(os.getenv("LIFE_OS_PROCESS_ACTION_BUDGET", "4"))

SAFE_ORPHAN_PROCESS_NAMES = frozenset({
    "opencode.exe",
    "node_repl.exe",
    "codex.exe",
})

@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    name: str

def _is_windows() -> bool:
    return os.name == "nt"

def memory_used_percent() -> float:
    if _is_windows():
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return float(status.dwMemoryLoad)
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        values: dict[str, int] = {}

        for line in meminfo.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            try:
                values[key] = int(rest.strip().split()[0])
            except (ValueError, IndexError):
                continue
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total:
            return round((1 - available / total) * 100, 1)
    return 0.0

def disk_free_percent(path: Path | None = None) -> float:
    target = path or Path.home()
    usage = shutil.disk_usage(target)
    if usage.total <= 0:
        return 0.0
    return round(usage.free / usage.total * 100, 1)

def foreground_pid() -> int | None:
    if not _is_windows():
        return None
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None

def windows_processes() -> list[ProcessInfo]:
    if not _is_windows():
        return []
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot failed")

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    items: list[ProcessInfo] = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            items.append(
                ProcessInfo(
                    pid=int(entry.th32ProcessID),
                    parent_pid=int(entry.th32ParentProcessID),
                    name=str(entry.szExeFile),
                )
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return items

def select_safe_orphans(
    processes: Iterable[ProcessInfo],
    *,
    foreground: int | None,
    current_pid: int | None = None,
) -> list[ProcessInfo]:
    items = list(processes)
    live_pids = {item.pid for item in items}
    own_pid = current_pid if current_pid is not None else os.getpid()

    candidates = [
        item
        for item in items
        if item.name.lower() in SAFE_ORPHAN_PROCESS_NAMES
        and item.pid not in {0, 4, own_pid, foreground}
        and item.parent_pid not in live_pids
        and item.parent_pid not in {0, 4}
    ]
    return sorted(candidates, key=lambda item: (item.name.lower(), item.pid))

def terminate_process(pid: int) -> bool:
    if not _is_windows():
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["taskkill", "/PID", str(pid)],
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=flags,
        check=False,
    )
    return result.returncode == 0

def cleanup_old_temp_files(
    *,
    now_epoch: float,
    retention_days: int = TEMP_RETENTION_DAYS,
    budget: int = TEMP_DELETE_BUDGET,
) -> tuple[int, int]:
    root = Path(tempfile.gettempdir())
    cutoff = now_epoch - retention_days * 86400

    deleted = 0
    freed = 0
    try:
        entries = os.scandir(root)
    except OSError:
        return deleted, freed
    with entries:
        for entry in entries:
            if deleted >= max(0, budget):
                break
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                if stat.st_mtime >= cutoff:
                    continue
                size = stat.st_size
                Path(entry.path).unlink(missing_ok=True)
                deleted += 1
                freed += size
            except OSError:
                continue
    return deleted, freed

def run() -> dict[str, object]:
    import time

    before_memory = round(memory_used_percent(), 1)
    before_disk_free = disk_free_percent()
    memory_pressure = before_memory >= MEMORY_PRESSURE_PCT
    disk_pressure = before_disk_free <= DISK_FREE_PRESSURE_PCT
    actions: list[dict[str, object]] = []

    if memory_pressure and _is_windows():
        processes = windows_processes()
        candidates = select_safe_orphans(
            processes,
            foreground=foreground_pid(),
        )
        for item in candidates[:PROCESS_ACTION_BUDGET]:
            closed = terminate_process(item.pid)
            actions.append({
                "kind": "close_safe_orphan",
                "pid": item.pid,
                "name": item.name,
                "closed": closed,
            })

    temp_deleted = 0
    temp_freed = 0
    if memory_pressure or disk_pressure:
        temp_deleted, temp_freed = cleanup_old_temp_files(now_epoch=time.time())
        if temp_deleted:
            actions.append({
                "kind": "cleanup_temp_files",
                "deleted": temp_deleted,
                "freed_bytes": temp_freed,
            })

    after_memory = round(memory_used_percent(), 1)
    after_disk_free = disk_free_percent()
    return {
        "memory_used_pct_before": before_memory,
        "memory_used_pct_after": after_memory,
        "disk_free_pct_before": before_disk_free,
        "disk_free_pct_after": after_disk_free,
        "memory_pressure": memory_pressure,
        "disk_pressure": disk_pressure,
        "actions": actions,
        "temp_files_deleted": temp_deleted,
        "temp_bytes_freed": temp_freed,
    }
