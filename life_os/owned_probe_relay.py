"""Start a native health endpoint only after its parent owns this process.

This trusted relay reads exactly one gate line without buffering the endpoint's
JSON protocol. On Windows its descendants inherit the already assigned kernel
job. On POSIX exec preserves the process group established by the parent.
"""
from __future__ import annotations

import os
import subprocess
import sys


def main():
    if len(sys.argv) < 3:
        return 87
    expected = sys.argv[1].encode("ascii") + b"\n"
    received = bytearray()
    while len(received) < len(expected):
        value = os.read(sys.stdin.fileno(), 1)
        if not value:
            return 87
        received.extend(value)
        if value == b"\n":
            break
    if bytes(received) != expected:
        return 87
    argv = sys.argv[2:]
    if os.name != "nt":
        os.execvp(argv[0], argv)
    # Explicit handles survive CREATE_NO_WINDOW and Python's non-inheritable
    # standard descriptors; passing None can lose the endpoint's stdin.
    child = subprocess.Popen(argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
