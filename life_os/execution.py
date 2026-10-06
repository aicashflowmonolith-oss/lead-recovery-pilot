"""Policy-gated observe -> plan -> act -> verify execution contracts.

This module deliberately contains no GUI/browser implementation.  It is the
small, deterministic safety boundary every native executor must pass through.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ActionClass(StrEnum):
    READ = "read"
    REVERSIBLE = "reversible"
    CONSEQUENTIAL = "consequential"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class Action:
    capability: str
    operation: str
    classification: ActionClass
    target: str = ""
    parameters: dict[str, Any] | None = None
    correlation_id: str = ""


@dataclass(frozen=True)
class Observation:
    state: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True)
class Verification:
    ok: bool
    observed: Observation
    reason: str = ""


class Executor(Protocol):
    def observe(self, action: Action) -> Observation: ...
    def act(self, action: Action) -> None: ...
    def verify(self, action: Action, before: Observation) -> Verification: ...


class ExecutionDenied(RuntimeError):
    """Raised before a side effect when policy does not authorize it."""


def authorize(action: Action, *, capability_allowed: bool,
              owner_approval: bool = False, emergency_stop: bool = False) -> None:
    """Fail closed before any side effect.

    Reads and reversible actions may run only through an explicitly allowed
    capability. Consequential/destructive actions additionally require an
    explicit approval supplied by the approval subsystem. Emergency stop wins
    over every other signal.
    """
    if emergency_stop:
        raise ExecutionDenied("emergency stop active")
    if not capability_allowed:
        raise ExecutionDenied("capability is not allowlisted")
    if action.classification in {ActionClass.CONSEQUENTIAL, ActionClass.DESTRUCTIVE} and not owner_approval:
        raise ExecutionDenied("owner approval required")


def execute(executor: Executor, action: Action, *, capability_allowed: bool,
            owner_approval: bool = False, emergency_stop: bool = False) -> Verification:
    """Execute exactly one policy-authorized action with state verification."""
    authorize(action, capability_allowed=capability_allowed,
              owner_approval=owner_approval, emergency_stop=emergency_stop)
    before = executor.observe(action)
    executor.act(action)
    result = executor.verify(action, before)
    if not isinstance(result, Verification):
        raise TypeError("executor.verify must return Verification")
    return result
