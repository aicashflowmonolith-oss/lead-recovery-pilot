"""TEAGAN-PRINCIPAL: deterministic owner-proxy governance for LIFE OS.

This module mirrors MONOLITH's principal constitution so the local owner-facing
front door and the business execution system share one authority contract.
It delegates routine work but never overrides downstream policy, budgets,
verification, rollback, or non-delegable human identity/consent boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
import sqlite3
from typing import Any


PRINCIPAL_ID = "teagan-principal"
CONSTITUTION_VERSION = "1.0"

CONSTITUTION: dict[str, Any] = {
    "principal_id": PRINCIPAL_ID,
    "version": CONSTITUTION_VERSION,
    "mission": "Minimize owner involvement while preserving deterministic authority boundaries.",
    "rules": {
        "routine_reversible_work": "delegate",
        "research_analysis_planning": "delegate",
        "code_test_verify": "delegate",
        "provider_failover_and_retries": "delegate_with_policy",
        "external_communication": "delegate_with_policy_and_disclose_agent",
        "financial_actions": "delegate_with_policy_and_budget",
        "destructive_or_high_impact_changes": "delegate_with_policy_and_verification",
        "identity_legal_consent_or_attestation": "human_required",
        "physical_world_actions_without_an_executor": "human_required",
        "false_impersonation_or_fabricated_consent": "deny",
    },
    "invariants": [
        "never treat missing evidence as success",
        "never override downstream authorization or policy",
        "never fabricate identity, consent, signatures, attestations, or evidence",
        "never mark consequential work complete without independent verification",
        "prefer reversible and idempotent execution",
        "route around unavailable providers instead of waiting on one provider",
    ],
}
CONSTITUTION_HASH = hashlib.sha256(
    json.dumps(CONSTITUTION, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


class AuthorityMode(StrEnum):
    AUTO = "AUTO"
    POLICY = "POLICY"
    HUMAN = "HUMAN"
    DENY = "DENY"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class PrincipalDecision:
    decision_id: str
    principal_id: str
    constitution_version: str
    constitution_hash: str
    action_class: str
    authority: AuthorityMode
    risk: RiskLevel
    requires_human: bool
    should_execute: bool
    reason: str
    world_state_fingerprint: str
    created_at: str

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.principal_id,
            "decision_id": self.decision_id,
            "constitution_version": self.constitution_version,
            "constitution_hash": self.constitution_hash,
            "action_class": self.action_class,
            "authority": self.authority.value,
            "risk": self.risk.value,
            "requires_human": self.requires_human,
            "world_state_fingerprint": self.world_state_fingerprint,
        }


_IMPERSONATION = re.compile(
    r"\b(pretend to be me|impersonate me|forge (?:my )?(?:signature|consent)|"
    r"fake (?:my )?(?:signature|consent|approval)|fabricate (?:my )?(?:consent|approval|identity))\b",
    re.I,
)
_IDENTITY_LEGAL = re.compile(
    r"\b(identity verification|verify my identity|government[- ]issued id|government id|"
    r"biometric|fingerprint scan|face scan|sign (?:this|the) (?:form|contract|agreement)|"
    r"signature required|attest|swear under oath|legal consent|accept terms on my behalf)\b",
    re.I,
)
_PHYSICAL = re.compile(
    r"\b(in person|physically (?:go|move|press|connect|disconnect|insert)|"
    r"show (?:my|your) (?:id|identification) in person|mail (?:this|the) physical|"
    r"plug (?:in|out) the (?:cable|device)|unplug the (?:cable|device)|"
    r"press the physical button|move the physical device)\b",
    re.I,
)
_FINANCIAL = re.compile(
    r"\b(pay|purchase|buy|order|subscribe|renew|charge|spend|payout|transfer money|"
    r"send money|refund|trade|invest)\b",
    re.I,
)
_EXTERNAL_COMMUNICATION = re.compile(
    r"\b(email|message|dm|contact|call|text|post|publish|send (?:a |the )?(?:message|email)|"
    r"reply to|respond to customer|outreach)\b",
    re.I,
)
_HIGH_IMPACT = re.compile(
    r"\b(delete|destroy|wipe|factory reset|deploy|production|merge|release|"
    r"change permissions|revoke access|rotate access|modify firewall|self[- ]modify)\b",
    re.I,
)
_PROVIDER_RECOVERY = re.compile(
    r"\b(failover|fallback|retry|reroute|route around|quota|usage limit|provider|backend|"
    r"worker offline|reconcile|recover)\b",
    re.I,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS principal_agent_meta (
  principal_id TEXT PRIMARY KEY,
  constitution_version TEXT NOT NULL,
  constitution_hash TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS principal_decisions (
  decision_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  constitution_version TEXT NOT NULL,
  constitution_hash TEXT NOT NULL,
  intent_hash TEXT NOT NULL,
  action_class TEXT NOT NULL,
  authority TEXT NOT NULL,
  risk TEXT NOT NULL,
  requires_human INTEGER NOT NULL,
  should_execute INTEGER NOT NULL,
  reason TEXT NOT NULL,
  world_state_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO principal_agent_meta(
          principal_id,constitution_version,constitution_hash,observed_at
        ) VALUES(?,?,?,?)
        ON CONFLICT(principal_id) DO UPDATE SET
          constitution_version=excluded.constitution_version,
          constitution_hash=excluded.constitution_hash,
          observed_at=excluded.observed_at
        """,
        (PRINCIPAL_ID, CONSTITUTION_VERSION, CONSTITUTION_HASH, now),
    )
    connection.commit()


class PrincipalAgent:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection
        self.db.row_factory = sqlite3.Row
        initialize(connection)

    def world_state_fingerprint(self) -> str:
        """Hash bounded local governance state without copying it into prompts."""
        records: list[list[object]] = []
        for sql in (
            "SELECT key,value FROM worker_state ORDER BY key",
            "SELECT id,action,risk,cost_cents,state FROM approvals ORDER BY id",
            "SELECT id,title,priority,status FROM goals ORDER BY id",
        ):
            try:
                rows = self.db.execute(sql).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                records.append([str(value) for value in tuple(row)])
        return hashlib.sha256(
            json.dumps(records, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def decide(self, intent: str, *, explicit_gate: bool = False) -> PrincipalDecision:
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("intent must be nonblank text")
        normalized = " ".join(intent.split())
        state_hash = self.world_state_fingerprint()
        action_class, authority, risk, human, execute, reason = self._classify(
            normalized, explicit_gate=explicit_gate
        )
        intent_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        basis = "|".join(
            [
                PRINCIPAL_ID,
                CONSTITUTION_VERSION,
                CONSTITUTION_HASH,
                intent_hash,
                state_hash,
                str(int(explicit_gate)),
                action_class,
                authority.value,
            ]
        )
        decision_id = "principal-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]
        row = self.db.execute(
            "SELECT * FROM principal_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is not None:
            return self._from_row(row)
        created_at = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """
            INSERT INTO principal_decisions(
              decision_id,principal_id,constitution_version,constitution_hash,
              intent_hash,action_class,authority,risk,requires_human,should_execute,
              reason,world_state_fingerprint,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id,
                PRINCIPAL_ID,
                CONSTITUTION_VERSION,
                CONSTITUTION_HASH,
                intent_hash,
                action_class,
                authority.value,
                risk.value,
                int(human),
                int(execute),
                reason,
                state_hash,
                created_at,
            ),
        )
        self.db.commit()
        return PrincipalDecision(
            decision_id=decision_id,
            principal_id=PRINCIPAL_ID,
            constitution_version=CONSTITUTION_VERSION,
            constitution_hash=CONSTITUTION_HASH,
            action_class=action_class,
            authority=authority,
            risk=risk,
            requires_human=human,
            should_execute=execute,
            reason=reason,
            world_state_fingerprint=state_hash,
            created_at=created_at,
        )

    def _classify(
        self, intent: str, *, explicit_gate: bool
    ) -> tuple[str, AuthorityMode, RiskLevel, bool, bool, str]:
        if explicit_gate:
            return (
                "EXPLICIT_OWNER_GATE",
                AuthorityMode.HUMAN,
                RiskLevel.HIGH,
                True,
                False,
                "An existing deterministic gate requires the owner.",
            )
        if _IMPERSONATION.search(intent):
            return (
                "FALSE_IDENTITY_OR_CONSENT",
                AuthorityMode.DENY,
                RiskLevel.HIGH,
                False,
                False,
                "The principal may act as a disclosed agent but may not fabricate identity or consent.",
            )
        if _IDENTITY_LEGAL.search(intent):
            return (
                "IDENTITY_OR_LEGAL_ATTESTATION",
                AuthorityMode.HUMAN,
                RiskLevel.HIGH,
                True,
                False,
                "Identity verification, legal attestation, signatures, and personal consent remain non-delegable.",
            )
        if _PHYSICAL.search(intent):
            return (
                "PHYSICAL_WORLD_ACTION",
                AuthorityMode.HUMAN,
                RiskLevel.MEDIUM,
                True,
                False,
                "No verified physical executor is attached for this action.",
            )
        if _FINANCIAL.search(intent):
            return (
                "FINANCIAL_ACTION",
                AuthorityMode.POLICY,
                RiskLevel.HIGH,
                False,
                True,
                "Delegate only through downstream financial authority, budget, and verification controls.",
            )
        if _EXTERNAL_COMMUNICATION.search(intent):
            return (
                "EXTERNAL_COMMUNICATION",
                AuthorityMode.POLICY,
                RiskLevel.MEDIUM,
                False,
                True,
                "Delegate as a disclosed agent subject to communication policy and authorization.",
            )
        if _HIGH_IMPACT.search(intent):
            return (
                "HIGH_IMPACT_SYSTEM_CHANGE",
                AuthorityMode.POLICY,
                RiskLevel.HIGH,
                False,
                True,
                "Delegate through downstream authorization, verification, rollback, and policy gates.",
            )
        if _PROVIDER_RECOVERY.search(intent):
            return (
                "PROVIDER_RECOVERY",
                AuthorityMode.POLICY,
                RiskLevel.MEDIUM,
                False,
                True,
                "Provider recovery is delegated through adaptive routing and verification.",
            )
        return (
            "ROUTINE_OPERATIONAL",
            AuthorityMode.AUTO,
            RiskLevel.LOW,
            False,
            True,
            "Routine reversible work is delegated to LIFE OS/MONOLITH.",
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PrincipalDecision:
        return PrincipalDecision(
            decision_id=str(row["decision_id"]),
            principal_id=str(row["principal_id"]),
            constitution_version=str(row["constitution_version"]),
            constitution_hash=str(row["constitution_hash"]),
            action_class=str(row["action_class"]),
            authority=AuthorityMode(str(row["authority"])),
            risk=RiskLevel(str(row["risk"])),
            requires_human=bool(row["requires_human"]),
            should_execute=bool(row["should_execute"]),
            reason=str(row["reason"]),
            world_state_fingerprint=str(row["world_state_fingerprint"]),
            created_at=str(row["created_at"]),
        )


__all__ = [
    "AuthorityMode",
    "CONSTITUTION",
    "CONSTITUTION_HASH",
    "CONSTITUTION_VERSION",
    "PRINCIPAL_ID",
    "PrincipalAgent",
    "PrincipalDecision",
    "RiskLevel",
    "initialize",
]
