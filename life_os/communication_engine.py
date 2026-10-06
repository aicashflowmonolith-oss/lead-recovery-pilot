"""Closed-loop communication, negotiation, sales and persuasion-defense engine.

All planning is deterministic, local-first, privacy-minimized, and subordinate to
consent, truthfulness, owner policy, and existing LIFE OS execution gates.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .events import append_event
from .influence import BASE_GUARDRAILS, TECHNIQUE_BY_KEY, build_plan

STATES = {"discovery","rapport","qualification","proposal","objection","negotiation","commitment","follow_up","conflict","deescalation","boundary","exit"}
SALES_STAGES = {"outreach","discovery","qualification","proposal","objection","negotiation","booking","close","follow_up","exit"}
HIGH_RISK_FLAGS = {"sexual_consent","romantic_pressure","medical_decision","legal_decision","financial_decision","minor","intoxicated","crisis","severe_distress","power_imbalance"}
RESTRICTED_TECHNIQUES = {"micro_commitment","honest_scarcity","truthful_social_proof","legitimate_authority","context_framing","reciprocity","shared_identity"}
SAFE_TECHNIQUES = {"active_listening","open_questions_reflections","autonomy_support","common_ground"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS communication_outcomes(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 person_hash TEXT NOT NULL, arena TEXT NOT NULL, state TEXT NOT NULL,
 technique_key TEXT NOT NULL, score REAL NOT NULL CHECK(score BETWEEN -1 AND 1),
 outcome TEXT NOT NULL DEFAULT '', occurred_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_communication_outcomes_person
ON communication_outcomes(person_hash,arena,state,technique_key,occurred_at DESC);
CREATE TABLE IF NOT EXISTS communication_evidence(
 technique_key TEXT NOT NULL, source_ref TEXT NOT NULL,
 evidence_grade TEXT NOT NULL, supports INTEGER NOT NULL CHECK(supports IN (-1,0,1)),
 independent INTEGER NOT NULL CHECK(independent IN (0,1)), observed_at TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', PRIMARY KEY(technique_key,source_ref));
"""

@dataclass(frozen=True)
class NegotiationPosition:
    role: str
    target: float
    reservation: float
    batna: str
    opening_anchor: float | None = None


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def _ensure(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='communication_outcomes'").fetchone() is None:
        initialize(connection)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _person_hash(person_ref: str) -> str:
    if not isinstance(person_ref, str) or not person_ref.strip():
        raise ValueError("person_ref must not be empty")
    return hashlib.sha256(person_ref.strip().lower().encode()).hexdigest()


def classify_state(signals: dict[str, Any]) -> str:
    if not isinstance(signals, dict):
        raise ValueError("signals must be a mapping")
    if signals.get("opt_out") or signals.get("ended"):
        return "exit"
    if signals.get("threat") or signals.get("unsafe"):
        return "deescalation"
    if signals.get("boundary_violation"):
        return "boundary"
    if signals.get("conflict") or signals.get("angry"):
        return "conflict"
    if signals.get("objection"):
        return "objection"
    if signals.get("negotiating") or signals.get("counteroffer"):
        return "negotiation"
    if signals.get("committed") or signals.get("meeting_request") or signals.get("accepted"):
        return "commitment"
    if signals.get("proposal_presented"):
        return "proposal"
    if signals.get("qualified"):
        return "qualification"
    if signals.get("rapport"):
        return "rapport"
    if signals.get("follow_up"):
        return "follow_up"
    return "discovery"


def objection_type(text: str) -> str:
    value = (text or "").lower()
    groups = (
        ("price", ("price","cost","expensive","budget","afford")),
        ("timing", ("timing","later","busy","not now","too soon")),
        ("trust", ("trust","proof","results","credible","risk")),
        ("authority", ("boss","partner","team","approval","decision maker")),
        ("need", ("need","priority","already fine","not necessary")),
        ("fit", ("fit","specific","custom","relevant","match")),
        ("implementation", ("setup","workload","migration","integration","effort")),
        ("comparison", ("competitor","alternative","other option","compare")),
    )
    for key, words in groups:
        if any(word in value for word in words):
            return key
    return "unknown"


def high_risk_policy(flags: set[str] | tuple[str, ...] | list[str]) -> dict[str, Any]:
    normalized = {str(x).strip().lower() for x in flags if str(x).strip()}
    active = normalized & HIGH_RISK_FLAGS
    return {
        "active": bool(active),
        "flags": tuple(sorted(active)),
        "allowed_techniques": tuple(sorted(SAFE_TECHNIQUES)) if active else None,
        "blocked_techniques": tuple(sorted(RESTRICTED_TECHNIQUES)) if active else (),
        "rule": "Use factual clarification, listening, autonomy support, boundaries, or disengagement only; do not optimize compliance." if active else "standard",
    }


def technique_rankings(connection: sqlite3.Connection, *, person_ref: str, arena: str, state: str, candidates: list[str]) -> list[dict[str, Any]]:
    _ensure(connection)
    person = _person_hash(person_ref)
    grade_prior = {"A": 0.35, "B": 0.15, "C": 0.0}
    result = []
    for key in candidates:
        technique = TECHNIQUE_BY_KEY[key]
        row = connection.execute("""SELECT COUNT(*) n,AVG(score) avg_score FROM communication_outcomes
            WHERE person_hash=? AND arena=? AND state=? AND technique_key=?""", (person,arena,state,key)).fetchone()
        n = int(row["n"] or 0); avg = float(row["avg_score"] or 0.0)
        personalized = avg * min(1.0, n / 5.0)
        result.append({"technique": key, "samples": n, "mean_outcome": round(avg,3),
                       "score": round(grade_prior[technique.evidence_grade] + personalized,3),
                       "evidence_grade": technique.evidence_grade})
    return sorted(result, key=lambda x: (-x["score"], -x["samples"], x["technique"]))


def closed_loop_plan(connection: sqlite3.Connection, *, person_ref: str, arena: str, objective: str,
                     signals: dict[str, Any] | None = None, context_flags: list[str] | tuple[str,...] = ()) -> dict[str, Any]:
    state = classify_state(signals or {})
    base = build_plan(arena=arena, objective=objective)
    policy = high_risk_policy(context_flags)
    candidates = list(base.technique_keys)
    if policy["active"]:
        candidates = [key for key in candidates if key in SAFE_TECHNIQUES]
        if "autonomy_support" not in candidates:
            candidates.append("autonomy_support")
        if "active_listening" not in candidates:
            candidates.append("active_listening")
    ranked = technique_rankings(connection, person_ref=person_ref, arena=arena, state=state, candidates=candidates)
    return {"state": state, "objective": objective.strip(), "techniques": [x["technique"] for x in ranked],
            "rankings": ranked, "guardrails": BASE_GUARDRAILS, "risk_policy": policy}


def record_result(connection: sqlite3.Connection, *, person_ref: str, arena: str, state: str,
                  technique_key: str, score: float, outcome: str = "") -> int:
    _ensure(connection)
    if state not in STATES: raise ValueError("unknown communication state")
    if technique_key not in TECHNIQUE_BY_KEY: raise ValueError("unknown influence technique")
    if type(score) not in (int,float) or not math.isfinite(score) or not -1 <= score <= 1:
        raise ValueError("score must be -1..1")
    cursor = connection.execute("""INSERT INTO communication_outcomes(person_hash,arena,state,technique_key,score,outcome,occurred_at)
        VALUES(?,?,?,?,?,?,?)""", (_person_hash(person_ref),arena,state,technique_key,float(score),(outcome or "")[:160],_now()))
    connection.commit()
    append_event(connection,"communication.outcome_recorded",{"arena":arena,"state":state,"technique":technique_key,"score":float(score)})
    return int(cursor.lastrowid)


def evaluate_offer(position: NegotiationPosition, offer: float) -> dict[str, Any]:
    if position.role not in {"buyer","seller"}: raise ValueError("role must be buyer or seller")
    if not all(math.isfinite(float(x)) for x in (position.target,position.reservation,offer)):
        raise ValueError("numeric negotiation values required")
    acceptable = offer <= position.reservation if position.role == "buyer" else offer >= position.reservation
    target_met = offer <= position.target if position.role == "buyer" else offer >= position.target
    if target_met:
        action = "accept_or_verify_terms"
    elif acceptable:
        action = "counter_or_accept_based_on_nonprice_terms"
    else:
        action = "decline_or_use_batna"
    return {"acceptable":acceptable,"target_met":target_met,"action":action,"batna":position.batna,
            "rule":"Never worsen the deal past the reservation point merely to preserve rapport or sunk cost."}


def concession_step(*, opening: float, target: float, step_index: int, total_steps: int = 4) -> float:
    if total_steps < 1 or not 0 <= step_index <= total_steps: raise ValueError("invalid concession step")
    # Diminishing concessions: move quickly first, then increasingly less.
    fractions=(0.0,0.50,0.75,0.90,1.0)
    if total_steps != 4:
        fraction = step_index / total_steps
        fraction = 1 - (1-fraction)**2
    else:
        fraction = fractions[step_index]
    return round(opening + (target-opening)*fraction, 2)


def objection_plan(kind: str) -> dict[str, Any]:
    plans = {
        "price": ("Clarify the budget/value gap before changing price.", "What would need to be true for the value to justify the cost?"),
        "timing": ("Find the actual timing constraint and next decision point.", "What changes between now and when this becomes worth revisiting?"),
        "trust": ("Use verifiable evidence and reduce risk, not pressure.", "What evidence would make this credible enough to evaluate?"),
        "authority": ("Identify the real decision process.", "Who else needs to be comfortable with this and what do they need to see?"),
        "need": ("Test whether the problem is meaningful enough to solve.", "What happens if you leave this exactly as it is?"),
        "fit": ("Clarify requirements before pitching harder.", "Which requirement looks least matched right now?"),
        "implementation": ("Reduce operational uncertainty with concrete scope.", "Which part of implementation creates the most friction?"),
        "comparison": ("Compare decision criteria instead of attacking alternatives.", "Which two or three criteria will decide between the options?"),
        "unknown": ("Do not rebut yet; uncover the real objection.", "What is the main thing holding you back?"),
    }
    if kind not in plans: raise ValueError("unknown objection type")
    objective, question = plans[kind]
    return {"type":kind,"objective":objective,"question":question,"techniques":["active_listening","open_questions_reflections","autonomy_support"]}


def deescalation_plan(*, intensity: int, threat: bool = False) -> dict[str, Any]:
    if not 1 <= intensity <= 10: raise ValueError("intensity must be 1..10")
    if threat or intensity >= 9:
        return {"mode":"disengage","steps":["stop persuasion","create distance","state one clear boundary","use appropriate safety/support resources"],"optimize_agreement":False}
    steps=["lower pace and message volume","reflect the core concern without pretending certainty","ask one clarifying question","offer a pause or choice"]
    if intensity >= 6: steps.append("defer problem-solving until intensity drops")
    return {"mode":"deescalate","steps":steps,"optimize_agreement":False}


def boundary_plan(boundary: str, consequence: str = "disengage from the interaction") -> dict[str, Any]:
    if not boundary.strip(): raise ValueError("boundary must not be empty")
    return {"structure":["state the observable issue","state the boundary once","state the action you control if it continues","follow through without arguing"],
            "boundary":boundary.strip(),"consequence":consequence.strip(),"rule":"A boundary controls your action; it is not a threat used to control someone else."}


def persuasion_defense(text: str) -> dict[str, Any]:
    value=(text or "").lower(); flags=[]
    patterns={
        "scarcity_pressure":("last chance","only today","right now","almost gone","limited spots"),
        "social_pressure":("everyone is doing","everyone knows","all your friends","people like you"),
        "authority_pressure":("trust me i'm an expert","because i said","doctor says","lawyer says"),
        "reciprocity_pressure":("after everything i did","you owe me","i did this for you"),
        "commitment_pressure":("you already agreed","you said yes before","don't back out"),
        "guilt_pressure":("if you cared","prove you love","you'd do it if"),
        "intimidation":("or else","you'll regret","better do","i'll make you"),
    }
    for key, phrases in patterns.items():
        if any(p in value for p in phrases): flags.append(key)
    return {"flags":flags,"pressure_detected":bool(flags),"response_rule":"Separate the claim from the pressure: verify facts, restore time/choice, compare alternatives, and decide from your own criteria."}


def practice_drill(skill: str, difficulty: int = 1) -> dict[str, Any]:
    if not 1 <= difficulty <= 3: raise ValueError("difficulty must be 1..3")
    drills={
        "objection":("A prospect says the offer costs too much and they are not convinced it will work.",["do not defend immediately","identify the real objection","ask one calibrated question","preserve refusal"]),
        "negotiation":("A counterparty makes an offer outside your target but near your reservation point.",["state BATNA and reservation point first","avoid reactive concession","trade rather than give","verify final terms"]),
        "boundary":("Someone repeatedly pushes after you already said no.",["repeat the boundary once","do not over-explain","state the action you control","follow through"]),
        "deescalation":("A conversation becomes defensive and heated.",["lower intensity","reflect the concern","stop persuasion","offer pause/choice"]),
        "sales_discovery":("A lead replied with mild interest but the problem, authority and urgency are unknown.",["ask open questions","qualify before proposing","do not manufacture urgency","agree a small next step only if useful"]),
    }
    if skill not in drills: raise ValueError("unknown practice skill")
    scenario,criteria=drills[skill]
    return {"skill":skill,"difficulty":difficulty,"scenario":scenario,"success_criteria":criteria,"extra_constraint":("Handle ambiguity without adding pressure." if difficulty>=2 else "")}


def sales_stage_guidance(stage: str) -> dict[str, Any]:
    if stage not in SALES_STAGES: raise ValueError("unknown sales stage")
    mapping={
        "outreach":("earn attention with relevance and a low-friction truthful ask",("active_listening","micro_commitment")),
        "discovery":("understand problem, impact, process and desired outcome",("open_questions_reflections","active_listening")),
        "qualification":("verify need, fit, authority, timing and ability to proceed",("open_questions_reflections","autonomy_support")),
        "proposal":("connect the verified need to a clear offer and material terms",("context_framing","truthful_social_proof","legitimate_authority")),
        "objection":("diagnose before responding; resolve only the real objection",("active_listening","open_questions_reflections","autonomy_support")),
        "negotiation":("protect reservation value and trade concessions for value",("active_listening","context_framing","autonomy_support")),
        "booking":("convert mutual interest into one explicit next step",("micro_commitment","autonomy_support")),
        "close":("verify terms, consent and next actions without artificial urgency",("autonomy_support","legitimate_authority")),
        "follow_up":("add useful context and make the next action easy to accept or decline",("reciprocity","micro_commitment","autonomy_support")),
        "exit":("stop contact cleanly and preserve suppression/opt-out state",("autonomy_support",)),
    }
    objective, techniques=mapping[stage]
    return {"stage":stage,"objective":objective,"techniques":list(techniques),"guardrails":BASE_GUARDRAILS}


def sales_guidance_for_classification(classification: str) -> dict[str, Any]:
    stage={"reply":"discovery","buyer_signal":"qualification","meeting_request":"booking","opt_out":"exit","bounce":"exit","auto_reply":"follow_up","payment_notice":"close","sent":"follow_up"}.get(classification,"discovery")
    return sales_stage_guidance(stage)


def register_evidence(connection: sqlite3.Connection, *, technique_key: str, source_ref: str, evidence_grade: str,
                      supports: int, independent: bool, observed_at: str | None = None, note: str = "") -> None:
    _ensure(connection)
    if technique_key not in TECHNIQUE_BY_KEY: raise ValueError("unknown technique")
    if evidence_grade not in {"A","B","C"}: raise ValueError("invalid evidence grade")
    if supports not in {-1,0,1}: raise ValueError("supports must be -1, 0 or 1")
    if not source_ref.strip(): raise ValueError("source_ref required")
    connection.execute("""INSERT INTO communication_evidence(technique_key,source_ref,evidence_grade,supports,independent,observed_at,note)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(technique_key,source_ref) DO UPDATE SET evidence_grade=excluded.evidence_grade,
        supports=excluded.supports,independent=excluded.independent,observed_at=excluded.observed_at,note=excluded.note""",
        (technique_key,source_ref.strip()[:500],evidence_grade,supports,int(independent),observed_at or _now(),note[:500]))
    connection.commit()


def evidence_review(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    _ensure(connection); out=[]
    for key, technique in TECHNIQUE_BY_KEY.items():
        rows=connection.execute("SELECT evidence_grade,supports,independent FROM communication_evidence WHERE technique_key=?",(key,)).fetchall()
        independent_positive=sum(1 for r in rows if r["independent"] and r["supports"]>0)
        independent_negative=sum(1 for r in rows if r["independent"] and r["supports"]<0)
        status="maintain"
        if independent_negative >= 2 and independent_negative > independent_positive: status="downgrade_review"
        elif independent_positive >= 2 and technique.evidence_grade == "C": status="upgrade_review"
        out.append({"technique":key,"current_grade":technique.evidence_grade,"sources":len(rows),
                    "independent_positive":independent_positive,"independent_negative":independent_negative,"status":status})
    append_event(connection,"communication.evidence_reviewed",{"techniques":len(out),"requires_review":sum(x["status"]!="maintain" for x in out)})
    return out
