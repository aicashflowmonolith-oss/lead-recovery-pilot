"""Central constitutional boundaries shared by LIFE OS subsystems.

These checks do not grant authority. They prevent unsafe data crossing and make
high-consequence categories explicit so individual adapters cannot silently
weaken the control plane.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_EXTERNAL_PAYLOAD_BYTES = 64 * 1024
MAX_EXTERNAL_STRING_CHARS = 16_000
MAX_CONTAINER_ITEMS = 1_000
MAX_DEPTH = 16

_SENSITIVE_EXACT = {
    "password", "passwd", "passphrase", "token", "access_token", "refresh_token",
    "api_key", "apikey", "secret", "client_secret", "private_key", "recovery_code",
    "recovery_codes", "security_answer", "cvv", "cvc", "pin", "seed_phrase",
    "mnemonic", "account_number", "card_number",
}
_SENSITIVE_SUFFIXES = ("_password", "_token", "_secret", "_api_key", "_private_key")
_SAFE_REFERENCE_SUFFIXES = ("_ref", "_reference", "_id", "_key_name")

HIGH_CONSEQUENCE_PURPOSES = frozenset({
    "spending", "contract", "legal_commitment", "identity_verification",
    "medical_decision", "financial_transfer", "external_contact",
    "human_service_hire", "credential_change", "security_change",
    "sexual_or_romantic_consent",
})


@dataclass(frozen=True)
class BoundaryDecision:
    allowed: bool
    owner_gate_required: bool
    reason: str


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value).strip().lower())


def sensitive_key(key: Any) -> bool:
    name = _normalized_key(key)
    if name.endswith(_SAFE_REFERENCE_SUFFIXES):
        return False
    return name in _SENSITIVE_EXACT or name.endswith(_SENSITIVE_SUFFIXES)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if sensitive_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    return value


def validate_payload(value: Any, *, external: bool = True, _depth: int = 0) -> None:
    """Reject secrets, pathological nesting and oversized external payloads."""
    if _depth > MAX_DEPTH:
        raise ValueError("payload nesting exceeds boundary limit")
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ValueError("payload object exceeds boundary item limit")
        for key, item in value.items():
            if external and sensitive_key(key):
                raise ValueError("external payload may not contain credentials or raw secrets")
            validate_payload(item, external=external, _depth=_depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ValueError("payload list exceeds boundary item limit")
        for item in value:
            validate_payload(item, external=external, _depth=_depth + 1)
        return
    if isinstance(value, str) and len(value) > MAX_EXTERNAL_STRING_CHARS:
        raise ValueError("payload string exceeds boundary limit")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("payload contains unsupported boundary type")


def validate_external_payload(value: Any, *, max_bytes: int = MAX_EXTERNAL_PAYLOAD_BYTES) -> None:
    validate_payload(value, external=True)
    try:
        encoded = json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("external payload must be bounded JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError("external payload exceeds boundary byte limit")


def decision(*, purpose: str, explicit_owner_approval: bool = False, reversible: bool = True) -> BoundaryDecision:
    purpose = _normalized_key(purpose)
    if purpose in HIGH_CONSEQUENCE_PURPOSES:
        if explicit_owner_approval:
            return BoundaryDecision(True, True, "high-consequence action has explicit owner approval")
        return BoundaryDecision(False, True, "high-consequence action requires explicit owner approval")
    if not reversible:
        if explicit_owner_approval:
            return BoundaryDecision(True, True, "irreversible action has explicit owner approval")
        return BoundaryDecision(False, True, "irreversible action requires explicit owner approval")
    return BoundaryDecision(True, False, "bounded reversible action")
