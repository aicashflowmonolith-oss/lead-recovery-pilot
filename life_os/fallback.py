"""Bounded fallback across authorized executors."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, TypeVar
T=TypeVar("T")

class PolicyGate(RuntimeError):
    """Authorization or safety gate requiring resolution, not fallback."""

class RecoverableExecutorError(RuntimeError):
    """Provider/tool failure eligible for another authorized route."""

@dataclass(frozen=True)
class Attempt:
    executor:str
    outcome:str
    detail:str=""

@dataclass(frozen=True)
class FallbackResult(Generic[T]):
    value:T
    executor:str
    attempts:tuple[Attempt,...]

def run_with_fallback(candidates:Iterable[str], invoke:Callable[[str],T], *, max_attempts:int=3)->FallbackResult[T]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    attempts:list[Attempt]=[]
    seen:set[str]=set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        if len(attempts)>=max_attempts:
            break
        try:
            value=invoke(name)
        except PolicyGate as exc:
            attempts.append(Attempt(name,"policy_gate",str(exc)))
            raise
        except RecoverableExecutorError as exc:
            attempts.append(Attempt(name,"recoverable_failure",str(exc)))
            continue
        attempts.append(Attempt(name,"success"))
        return FallbackResult(value,name,tuple(attempts))
    detail="; ".join(f"{a.executor}: {a.detail}" for a in attempts) or "no authorized candidates"
    raise RecoverableExecutorError(f"authorized executor routes exhausted: {detail}")
