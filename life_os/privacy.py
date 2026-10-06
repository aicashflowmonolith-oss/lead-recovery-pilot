"""Redaction helpers for exports and integrations."""
from __future__ import annotations

from .boundaries import redact as _boundary_redact, sensitive_key

SENSITIVE_KEYS={"password","token","secret","api_key","recovery_code","address","health_note"}


def redact(value):
    # Preserve legacy explicitly-private fields while inheriting the central
    # credential/secret detector used by every external boundary.
    if isinstance(value,dict):
        return {k:("[REDACTED]" if str(k).lower() in SENSITIVE_KEYS or sensitive_key(k) else redact(v)) for k,v in value.items()}
    if isinstance(value,list): return [redact(x) for x in value]
    if isinstance(value,tuple): return tuple(redact(x) for x in value)
    return _boundary_redact(value)
