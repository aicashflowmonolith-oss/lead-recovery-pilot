"""Windows kernel ownership of worker descendants, including orphaned providers.

The job handle is non-inheritable. Windows closes it when the controller exits,
including an ungraceful kill, and terminates all assigned descendants. Workers
wait at a startup gate until assignment succeeds; no provider can escape during
the Popen/assignment window. No third-party runtime is required.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time


class ProcessContainmentError(BaseException):
    """Fatal lane invariant, never a transport error eligible for fallback.

    Ordinary handlers must leave the current worktree and running lease intact.
    The owning recovery controller contains the whole worker job and verifies
    exit before it may resume that original lease. BaseException intentionally
    bypasses broad provider/verification/worker retry catches.
    """

    def __init__(self, message, *, identities=None, snapshot_complete=False):
        super().__init__(message)
        self.identities = identities or []
        self.snapshot_complete = bool(snapshot_complete)


class WindowsJob:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows job ownership is unavailable on this platform")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k.IsProcessInJob.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL

        class BasicLimit(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("basic", BasicLimit), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_count(self):
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, wintypes.DWORD) for name in ("page_faults", "total", "active", "terminated")]
        data = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(data), ctypes.sizeof(data), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return data.active

    def owned_process_handles(self, *, deadline=None):
        """Snapshot wait handles, validating membership against PID reuse."""
        capacity = max(16, self.active_count() + 8)
        handles = {}
        if deadline is None:
            deadline = time.monotonic() + 3
        try:
            while capacity <= 65536:
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned process snapshot exceeded termination deadline")
                data = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
                if self.kernel.QueryInformationJobObject(self.handle, 3, data, ctypes.sizeof(data), None):
                    assigned = ctypes.c_ulong.from_buffer(data, 0).value
                    count = ctypes.c_ulong.from_buffer(data, 4).value
                    if assigned > count:
                        capacity = max(capacity * 2, assigned + 8)
                        continue
                    if count > capacity:
                        raise OSError("owned process snapshot returned an invalid count")
                    identities = (ctypes.c_size_t * count).from_buffer(data, 8)
                    for pid in identities:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("owned process snapshot exceeded termination deadline")
                        handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
                        if not handle:
                            if ctypes.get_last_error() == 87:  # Already exited.
                                continue
                            raise ctypes.WinError(ctypes.get_last_error())
                        member = wintypes.BOOL()
                        if not self.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                            error = ctypes.get_last_error()
                            self.kernel.CloseHandle(handle)
                            raise ctypes.WinError(error)
                        if member.value:
                            handles[int(pid)] = handle
                        else:
                            self.kernel.CloseHandle(handle)
                    return handles
                if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise ctypes.WinError(ctypes.get_last_error())
                capacity *= 2
            raise OSError("owned process snapshot exceeded its bound")
        except BaseException:
            for handle in handles.values():
                self.kernel.CloseHandle(handle)
            raise

    def process_identities(self, handles):
        identities = []
        for pid, handle in handles.items():
            created, exited, kernel_time, user_time = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise ctypes.WinError(ctypes.get_last_error())
            identities.append({"pid": pid, "creation_time": (created.dwHighDateTime << 32) | created.dwLowDateTime})
        return identities

    def terminate(self, timeout=10, *, on_snapshot=None):
        # ActiveProcesses can reach zero before the process wait handles signal.
        # Hold identity-safe handles before termination and wait for actual exit,
        # so a stopped worker's unfinished job cannot overlap its replacement.
        deadline = time.monotonic()+timeout
        handles = {}
        identities = []
        snapshot_complete = False
        try:
            try:
                handles = self.owned_process_handles(deadline=deadline)
                identities = self.process_identities(handles)
                if on_snapshot:
                    on_snapshot(identities, False)
            finally:
                if not self.kernel.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            additional = self.owned_process_handles(deadline=deadline)
            for pid, handle in additional.items():
                if pid in handles:
                    self.kernel.CloseHandle(handle)
                else:
                    handles[pid] = handle
            identities = self.process_identities(handles)
            snapshot_complete = True
            if on_snapshot:
                on_snapshot(identities, True)
            while self.active_count():
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned process tree did not exit; job cannot resume")
                time.sleep(.01)
            for handle in handles.values():
                remaining_ms = max(0, int((deadline-time.monotonic())*1000))
                result = self.kernel.WaitForSingleObject(handle, remaining_ms)
                if result == 258:
                    raise TimeoutError("owned process exit was not verified; job cannot resume")
                if result != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
        except Exception as exc:
            raise ProcessContainmentError("Owned process exit was not verified; stop lane and preserve unfinished work",
                                          identities=identities, snapshot_complete=snapshot_complete) from exc
        finally:
            for handle in handles.values():
                self.kernel.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
