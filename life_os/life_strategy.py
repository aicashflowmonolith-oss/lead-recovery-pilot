"""Whole-life strategy, decision governance, and multi-base planning.

The location planner is one subsystem. The governing objective above it is to
maximize durable whole-life flourishing across every LIFE OS domain while
preventing single-metric optimization from sacrificing critical life areas.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .foundation import create_entity, link_entities
from .ontology import DOMAIN_SPECS, normalize_domain

LIFE_OBJECTIVE_ENTITY_ID = "policy:best-life-governing-objective"
LIFE_OBJECTIVE_VERSION = 3
STRATEGY_ENTITY_ID = "policy:global-life-architecture"
STRATEGY_VERSION = 2

# Cross-domain human outcomes used for actual decision scoring. Canonical LIFE OS
# domains are broader and are all governed by the top-level policy below.
LIFE_WEIGHTS: dict[str, float] = {
    "health": 0.17,
    "freedom": 0.11,
    "money": 0.10,
    "relationships": 0.11,
    "meaning": 0.09,
    "time": 0.08,
    "growth": 0.07,
    "environment": 0.06,
    "experiences": 0.06,
    "safety": 0.07,
    "recreation": 0.03,
    "appearance": 0.02,
    "optionality": 0.03,
}

# These are minimum acceptable floors, not targets. A choice that drops a
# critical area below its floor is rejected even if it scores extremely well in
# money, productivity, status, or another single dimension.
DEFAULT_LIFE_FLOORS: dict[str, float] = {
    "health": 35.0,
    "freedom": 25.0,
    "money": 20.0,
    "relationships": 20.0,
    "meaning": 20.0,
    "time": 20.0,
    "safety": 35.0,
}

# Critical infrastructure and essential primary assets cannot win on lifestyle
# score alone. They need current evidence that they are dependable and recoverable.
CRITICAL_QUALITY_FLOORS: dict[str, float] = {
    "reliability": 75.0,
    "recoverability": 65.0,
    "supportability": 60.0,
    "evidence_strength": 0.60,
}

# Location-specific scoring remains intentionally narrower because it is only
# evaluating places, not the whole life.
DEFAULT_WEIGHTS: dict[str, float] = {
    "health": 0.20,
    "wealth": 0.18,
    "freedom": 0.18,
    "relationships": 0.12,
    "experiences": 0.10,
    "resilience": 0.12,
    "infrastructure": 0.10,
}

DEFAULT_ROLES = ("legal_anchor", "operations", "opportunity")


@dataclass(frozen=True)
class LifeChoice:
    """A proposed action scored against the full-life objective.

    ``critical_system`` should be true for choices whose failure can strand the
    owner or materially disable LIFE/MONOLITH: primary transport, phone/service,
    control-plane infrastructure, primary compute/storage, identity/auth paths,
    and similar essential assets. Those choices fail closed unless their
    reliability, recoverability, supportability, and evidence are proven.
    """

    name: str
    scores: Mapping[str, float]
    reversibility: float = 50.0
    downside_risk: float = 0.0
    confidence: float = 1.0
    hard_gate_ok: bool = True
    critical_system: bool = False
    reliability: float | None = None
    recoverability: float | None = None
    supportability: float | None = None
    evidence_strength: float | None = None
    decision_evidence_current: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("life choice name is required")
        missing = set(LIFE_WEIGHTS) - set(self.scores)
        if missing:
            raise ValueError(f"missing whole-life scores: {sorted(missing)}")
        if any(not 0 <= float(self.scores[k]) <= 100 for k in LIFE_WEIGHTS):
            raise ValueError("whole-life scores must be 0..100")
        if not 0 <= self.reversibility <= 100:
            raise ValueError("reversibility must be 0..100")
        if not 0 <= self.downside_risk <= 100:
            raise ValueError("downside_risk must be 0..100")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be 0..1")
        for key in ("reliability", "recoverability", "supportability"):
            value = getattr(self, key)
            if value is not None and not 0 <= float(value) <= 100:
                raise ValueError(f"{key} must be 0..100")
        if self.evidence_strength is not None and not 0 <= self.evidence_strength <= 1:
            raise ValueError("evidence_strength must be 0..1")


@dataclass(frozen=True)
class LifeChoiceAssessment:
    name: str
    score: float | None
    blockers: tuple[str, ...]
    weakest_dimension: str
    weakest_score: float


@dataclass(frozen=True)
class LifeResponsibility:
    """Any real-life responsibility or desired outcome presented to LIFE OS."""

    title: str
    domain: str | None = None
    software_possible: bool = True
    tool_available: bool = True
    authorization_present: bool = True
    legally_requires_personal_action: bool = False
    identity_verification_required: bool = False
    owner_physical_presence_required: bool = False

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("life responsibility title is required")


@dataclass(frozen=True)
class ResponsibilityRoute:
    title: str
    domain: str
    covered: bool
    execution_mode: str
    blockers: tuple[str, ...]


def route_life_responsibility(item: LifeResponsibility) -> ResponsibilityRoute:
    """Route every life item without allowing an unsupported/out-of-scope state.

    Unknown areas are retained under ``unclassified``. Missing software or connector
    capability becomes a capability gap for the system to close. Human involvement is
    reserved for actions that truly require the owner personally, physical presence,
    identity verification, or missing authorization for an external action.
    """
    domain = normalize_domain(item.domain)
    blockers: list[str] = []
    if item.legally_requires_personal_action:
        blockers.append("legally_requires_personal_action")
    if item.identity_verification_required:
        blockers.append("identity_verification_required")
    if item.owner_physical_presence_required or not item.software_possible:
        blockers.append("physical_world_action")
    if blockers:
        return ResponsibilityRoute(item.title, domain, True, "human_gate", tuple(dict.fromkeys(blockers)))
    if not item.authorization_present:
        return ResponsibilityRoute(item.title, domain, True, "human_gate", ("authorization_required",))
    if not item.tool_available:
        return ResponsibilityRoute(item.title, domain, True, "capability_gap", ("tool_or_connector_missing",))
    return ResponsibilityRoute(item.title, domain, True, "automate_now", ())


def _normalized_weights(weights: Mapping[str, float], required: set[str]) -> dict[str, float]:
    result = {k: float(v) for k, v in weights.items()}
    if set(result) != required:
        raise ValueError("weights must cover the canonical dimensions exactly")
    total = sum(result.values())
    if total <= 0 or any(v < 0 for v in result.values()):
        raise ValueError("weights must be non-negative with positive total")
    return {k: v / total for k, v in result.items()}


def _critical_quality_blockers(choice: LifeChoice) -> list[str]:
    if not choice.critical_system:
        return []
    blockers: list[str] = []
    if not choice.decision_evidence_current:
        blockers.append("stale_decision_evidence")
    for key, floor in CRITICAL_QUALITY_FLOORS.items():
        value = getattr(choice, key)
        if value is None:
            blockers.append(f"unproven:{key}")
        elif float(value) < floor:
            blockers.append(f"below_critical_floor:{key}")
    return blockers


def assess_life_choice(
    choice: LifeChoice,
    *,
    weights: Mapping[str, float] | None = None,
    floors: Mapping[str, float] | None = None,
) -> LifeChoiceAssessment:
    """Score a decision only when it preserves life and critical-system floors.

    The score uses a weighted geometric mean so one weak area cannot be hidden by
    an extreme strength elsewhere. Uncertain projections are pulled toward a
    neutral score of 50. Reversibility receives a modest bonus; downside risk a
    larger penalty so irreversible high-downside bets must earn their place.

    Critical systems also fail closed on stale or missing operational-quality
    evidence. This prevents an exciting but fragile vehicle/provider/device from
    beating a proven option merely because its lifestyle scores are high.
    """
    normalized = _normalized_weights(weights or LIFE_WEIGHTS, set(LIFE_WEIGHTS))
    active_floors = dict(DEFAULT_LIFE_FLOORS if floors is None else floors)
    unknown_floors = set(active_floors) - set(LIFE_WEIGHTS)
    if unknown_floors:
        raise ValueError(f"unknown life-floor dimensions: {sorted(unknown_floors)}")
    if any(not 0 <= float(v) <= 100 for v in active_floors.values()):
        raise ValueError("life floors must be 0..100")

    projected = {
        key: 50.0 + (float(choice.scores[key]) - 50.0) * choice.confidence
        for key in LIFE_WEIGHTS
    }
    weakest_dimension = min(projected, key=projected.get)
    weakest_score = projected[weakest_dimension]
    blockers: list[str] = []
    if not choice.hard_gate_ok:
        blockers.append("hard_gate")
    for key, floor in active_floors.items():
        if projected[key] < float(floor):
            blockers.append(f"below_floor:{key}")
    blockers.extend(_critical_quality_blockers(choice))

    if blockers:
        return LifeChoiceAssessment(
            choice.name,
            None,
            tuple(blockers),
            weakest_dimension,
            round(weakest_score, 2),
        )

    # Clamp away from zero before taking logs. The floor system handles truly
    # unacceptable values; this calculation rewards balance above those floors.
    quality = math.exp(
        sum(normalized[k] * math.log(max(1.0, projected[k])) for k in normalized)
    )
    reversibility_bonus = (choice.reversibility - 50.0) * 0.04
    risk_penalty = choice.downside_risk * 0.10
    score = max(0.0, min(100.0, quality + reversibility_bonus - risk_penalty))
    return LifeChoiceAssessment(
        choice.name,
        round(score, 2),
        (),
        weakest_dimension,
        round(weakest_score, 2),
    )


def rank_life_choices(
    choices: Sequence[LifeChoice],
    *,
    weights: Mapping[str, float] | None = None,
    floors: Mapping[str, float] | None = None,
) -> list[LifeChoiceAssessment]:
    """Return valid choices best-first, followed by blocked choices."""
    assessed = [assess_life_choice(c, weights=weights, floors=floors) for c in choices]
    return sorted(
        assessed,
        key=lambda a: (a.score is not None, a.score if a.score is not None else -1.0),
        reverse=True,
    )


@dataclass(frozen=True)
class ComplianceState:
    visa_valid: bool = True
    business_activity_allowed: bool = True
    healthcare_covered: bool = True
    insurance_covered: bool = True
    visa_days_remaining: int | None = None
    tax_days_remaining: int | None = None
    tax_risk: str = "low"

    def blockers(self, planned_stay_days: int) -> tuple[str, ...]:
        if planned_stay_days <= 0:
            raise ValueError("planned_stay_days must be positive")
        out: list[str] = []
        if not self.visa_valid:
            out.append("visa_invalid")
        if not self.business_activity_allowed:
            out.append("business_activity_not_allowed")
        if not self.healthcare_covered:
            out.append("healthcare_not_covered")
        if not self.insurance_covered:
            out.append("insurance_not_covered")
        if self.visa_days_remaining is not None and self.visa_days_remaining < planned_stay_days:
            out.append("visa_day_limit")
        if self.tax_days_remaining is not None and self.tax_days_remaining < planned_stay_days:
            out.append("tax_residency_day_limit")
        if self.tax_risk.lower() == "high":
            out.append("high_tax_residency_risk")
        return tuple(out)


@dataclass(frozen=True)
class LocationCandidate:
    name: str
    country: str
    monthly_cost_cents: int
    scores: Mapping[str, float]
    roles: tuple[str, ...]
    compliance: ComplianceState = ComplianceState()
    planned_stay_days: int = 30

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.country.strip() or self.monthly_cost_cents < 0:
            raise ValueError("invalid location candidate")
        if self.planned_stay_days <= 0:
            raise ValueError("planned_stay_days must be positive")
        missing = set(DEFAULT_WEIGHTS) - set(self.scores)
        if missing:
            raise ValueError(f"missing whole-life scores: {sorted(missing)}")
        if any(not 0 <= float(self.scores[k]) <= 100 for k in DEFAULT_WEIGHTS):
            raise ValueError("whole-life scores must be 0..100")
        if not self.roles:
            raise ValueError("at least one base role is required")


@dataclass(frozen=True)
class PortfolioPlan:
    role_assignments: Mapping[str, str]
    base_names: tuple[str, ...]
    candidate_scores: Mapping[str, float]
    warnings: tuple[str, ...]


def _validated_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
    return _normalized_weights(weights or DEFAULT_WEIGHTS, set(DEFAULT_WEIGHTS))


def score_candidate(
    candidate: LocationCandidate,
    *,
    monthly_budget_cents: int,
    weights: Mapping[str, float] | None = None,
) -> float | None:
    """Return a 0..100 location score, or None when hard-gated."""
    if monthly_budget_cents <= 0:
        raise ValueError("monthly_budget_cents must be positive")
    if candidate.monthly_cost_cents > monthly_budget_cents:
        return None
    if candidate.compliance.blockers(candidate.planned_stay_days):
        return None
    normalized = _validated_weights(weights)
    quality = sum(float(candidate.scores[k]) * normalized[k] for k in normalized)
    headroom = 100.0 * (1.0 - candidate.monthly_cost_cents / monthly_budget_cents)
    return round(max(0.0, min(100.0, quality * 0.90 + headroom * 0.10)), 2)


def plan_multi_base(
    candidates: Sequence[LocationCandidate],
    *,
    monthly_budget_cents: int,
    roles: Sequence[str] = DEFAULT_ROLES,
    weights: Mapping[str, float] | None = None,
) -> PortfolioPlan:
    """Assign the best compliant candidate to each base role."""
    if not roles:
        raise ValueError("at least one role is required")
    scored: dict[str, float] = {}
    by_name: dict[str, LocationCandidate] = {}
    for candidate in candidates:
        score = score_candidate(candidate, monthly_budget_cents=monthly_budget_cents, weights=weights)
        if score is not None:
            scored[candidate.name] = score
            by_name[candidate.name] = candidate

    assignments: dict[str, str] = {}
    warnings: list[str] = []
    for role in roles:
        eligible = [c for c in by_name.values() if role in c.roles]
        if not eligible:
            warnings.append(f"no_compliant_candidate:{role}")
            continue
        best = max(eligible, key=lambda c: (scored[c.name], -c.monthly_cost_cents, c.name))
        assignments[role] = best.name

    base_names = tuple(dict.fromkeys(assignments.values()))
    return PortfolioPlan(assignments, base_names, scored, tuple(warnings))


def _upsert_policy(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    domain_key: str,
    title: str,
    priority: int,
    next_action: str,
    metadata: dict,
) -> None:
    row = connection.execute("SELECT id FROM canonical_entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        create_entity(
            connection,
            entity_id=entity_id,
            entity_type="personal_policy",
            domain_key=domain_key,
            title=title,
            priority=priority,
            fact_class="preference",
            confidence=1.0,
            next_action=next_action,
            metadata=metadata,
        )
        return
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """UPDATE canonical_entities SET domain_key=?,title=?,priority=?,fact_class='preference',
           confidence=1.0,next_action=?,metadata_json=?,updated_at=? WHERE id=?""",
        (
            domain_key,
            title,
            priority,
            next_action,
            json.dumps(metadata, separators=(",", ":"), sort_keys=True),
            now,
            entity_id,
        ),
    )
    connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """Persist the best-life governor plus its location sub-policy."""
    governed_domains = [spec.key for spec in DOMAIN_SPECS]
    objective_metadata = {
        "version": LIFE_OBJECTIVE_VERSION,
        "objective": "maximize durable whole-life flourishing across the present and future life",
        "universal_scope": True,
        "scope_rule": "every present, future, known, novel, digital, physical, administrative, financial, social, legal, health, work, home, or personal responsibility is in scope",
        "governed_domains": governed_domains,
        "catch_all_domain": "unclassified",
        "execution_contract": {
            "automate_now": "execute directly when software, authority, access, and safety constraints allow",
            "capability_gap": "retain ownership of the responsibility and create/obtain a safe capability rather than declaring it unsupported",
            "human_gate": "prepare everything possible, request only the irreducible personal action or authorization, then resume automatically",
            "out_of_scope": "forbidden",
        },
        "coverage_examples": [
            "taxes", "bills", "banking", "benefits", "insurance", "documents", "appointments",
            "health", "food", "home", "transportation", "travel", "work", "business", "learning",
            "relationships", "communication", "shopping", "maintenance", "security", "emergencies",
            "entertainment", "recreation", "appearance", "future planning", "estate and legacy",
        ],
        "decision_dimensions": list(LIFE_WEIGHTS),
        "weights": LIFE_WEIGHTS,
        "minimum_floors": DEFAULT_LIFE_FLOORS,
        "critical_quality_floors": CRITICAL_QUALITY_FLOORS,
        "commitment_revalidation": {
            "required": True,
            "rule": "specific products, providers, vehicles, locations, and other time-sensitive candidates are provisional until current authoritative evidence is rechecked at spend, sign, deploy, or other consequential commitment time",
            "critical_systems": "must have current evidence for reliability, recoverability, supportability, legality/coverage where applicable, and a non-fragile recovery path",
        },
        "principles": [
            "nothing in life is outside the system's coverage; unknown areas are retained until understood",
            "a missing tool, connector, workflow, or integration is a capability gap to solve, not a reason to drop responsibility",
            "when a human-only gate is unavoidable, prepare everything around it and minimize the required owner action",
            "optimize the whole life, never a single metric in isolation",
            "apply legality, safety, consent, and critical-health constraints before optimization",
            "critical infrastructure and essential primary assets must be proven reliable, recoverable, supportable, and based on current evidence before commitment",
            "where practical, critical capabilities need redundancy or a safe alternate path so one failure cannot strand the owner or control plane",
            "specific products, brands, providers, and models are candidates rather than permanent strategy; re-source and re-verify them when commitment becomes real",
            "primary essential capability comes before optional toys or duplicate luxury capability unless a measured exception improves whole-life value without weakening the base",
            "preserve minimum floors so money, productivity, status, or pleasure cannot compensate for severe damage elsewhere",
            "treat money as a tool for life quality, freedom, resilience, generosity, and future options rather than the terminal objective",
            "protect time, autonomy, attention, relationships, meaning, health, and sleep as scarce life resources",
            "prefer reversible experiments before irreversible commitments when evidence is incomplete",
            "penalize downside risk, fragility, hidden maintenance burden, and loss of optionality",
            "measure real outcomes after decisions and update beliefs instead of defending old plans",
            "automate repetitive work to create more human time and agency, not to optimize activity for its own sake",
            "seek compounding gains that improve multiple domains at once",
            "avoid Goodhart effects: no KPI is allowed to become the definition of a good life",
            "re-plan when age, income, health, relationships, environment, obligations, opportunities, values, prices, technology, laws, or provider quality materially change",
        ],
        "feedback_loop": [
            "observe current reality",
            "identify the largest whole-life bottleneck or opportunity",
            "generate feasible interventions",
            "reject options that violate hard constraints or critical floors",
            "for critical systems reject stale, unproven, fragile, unsupported, or unrecoverable candidates",
            "rank remaining options by balanced expected whole-life value",
            "revalidate time-sensitive evidence immediately before consequential commitment",
            "prefer the smallest reversible action that can validate the thesis",
            "measure actual outcomes",
            "update the model and repeat",
        ],
    }
    _upsert_policy(
        connection,
        entity_id=LIFE_OBJECTIVE_ENTITY_ID,
        domain_key="decision_engine",
        title="Best possible whole-life governing objective",
        priority=100,
        next_action="Continuously choose the highest expected whole-life improvement while protecting critical floors, system reliability, recoverability, and future optionality.",
        metadata=objective_metadata,
    )

    location_metadata = {
        "version": STRATEGY_VERSION,
        "parent_policy": LIFE_OBJECTIVE_ENTITY_ID,
        "objective": "use location as one controllable input to whole-life quality",
        "dimensions": list(DEFAULT_WEIGHTS),
        "weights": DEFAULT_WEIGHTS,
        "base_roles": list(DEFAULT_ROLES),
        "principles": [
            "keep a legally clean primary residency and tax setup",
            "use multiple locations when their distinct advantages improve total life quality",
            "prefer a low-cost launch or operations base while income is still being built",
            "use high-opportunity locations when networking, relationships, clients, or experiences justify the cost",
            "track visa-day, tax-residency, healthcare, insurance, travel-cost, and business-activity constraints",
            "do not recommend a stay when a hard legal, coverage, or affordability gate is unresolved",
            "avoid unnecessary permanent commitments when a reversible multi-base option preserves more freedom",
        ],
    }
    _upsert_policy(
        connection,
        entity_id=STRATEGY_ENTITY_ID,
        domain_key="future_design",
        title="Global multi-base whole-life architecture",
        priority=100,
        next_action="Continuously evaluate locations as one subsystem of the best-life objective.",
        metadata=location_metadata,
    )
    link_entities(
        connection,
        LIFE_OBJECTIVE_ENTITY_ID,
        "governs",
        STRATEGY_ENTITY_ID,
        metadata={"scope": "location_and_residency"},
    )
    connection.commit()