"""Governed communication and influence skills for LIFE OS.

This module treats influence as a communication skill, not a bypass around
another person's autonomy. It is deliberately lightweight and deterministic.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .events import append_event


EVIDENCE_GRADES = {"A", "B", "C"}
ARENAS = {"self", "relationship", "sales", "negotiation", "leadership", "service", "general"}


@dataclass(frozen=True)
class Technique:
    key: str
    title: str
    family: str
    evidence_grade: str
    purpose: str
    rule: str


@dataclass(frozen=True)
class InfluencePlan:
    arena: str
    objective: str
    technique_keys: tuple[str, ...]
    guardrails: tuple[str, ...]


TECHNIQUES = (
    Technique("active_listening", "Active listening", "FBI/Rogers", "A", "Understand first and reduce friction.", "Listen, paraphrase, label emotion carefully, and confirm before proposing action."),
    Technique("open_questions_reflections", "Open questions and reflections", "Motivational interviewing", "A", "Surface motives, constraints, and the person's own reasons.", "Ask open questions, reflect the answer, and avoid arguing someone into agreement."),
    Technique("autonomy_support", "Autonomy support", "Motivational interviewing", "A", "Keep change voluntary and internally owned.", "Make refusal easy and explicitly preserve the other person's choice."),
    Technique("micro_commitment", "Voluntary micro-commitments", "Foot-in-the-door / consistency", "A", "Reduce friction with a small relevant first step.", "Use only small, truthful, reversible requests that are aligned with the later request and easy to refuse."),
    Technique("reciprocity", "Reciprocity", "Cialdini", "A", "Lead with useful value before asking for value back.", "Give genuine value without creating a hidden debt or obligation."),
    Technique("truthful_social_proof", "Truthful social proof", "Cialdini", "A", "Reduce uncertainty with relevant evidence from similar others.", "Use real, representative evidence only; never fabricate popularity, reviews, demand, or consensus."),
    Technique("legitimate_authority", "Legitimate authority", "Cialdini", "A", "Make relevant expertise and trustworthiness visible.", "Use real credentials or evidence; never imply expertise, affiliation, or endorsement that does not exist."),
    Technique("honest_scarcity", "Honest scarcity", "Cialdini", "A", "Clarify a genuine constraint or expiring opportunity.", "Only state scarcity, deadlines, inventory, or consequences that are objectively true."),
    Technique("common_ground", "Common ground and liking", "Cialdini", "A", "Increase rapport through genuine similarity and respect.", "Find real shared interests or values; do not fake similarity."),
    Technique("shared_identity", "Shared identity", "Cialdini unity", "B", "Frame cooperation around a genuine shared group or goal.", "Use only authentic shared identity; never invent belonging or exploit protected identity."),
    Technique("context_framing", "Context framing", "Pre-suasion / framing", "B", "Direct attention to the most decision-relevant facts before a request.", "Frame selectively but truthfully; do not hide material downsides or alternatives."),
    Technique("behavior_baseline", "Behavior baseline and deviation", "Behavior observation", "C", "Notice changes that may justify a clarifying question.", "Treat behavior as a hypothesis generator only. Never call a person deceptive from a single cue, body-language sign, or deviation."),
)

TECHNIQUE_BY_KEY = {item.key: item for item in TECHNIQUES}

BASE_GUARDRAILS = (
    "No deception, impersonation, fabricated evidence, or hidden material facts.",
    "No threats, coercion, intimidation, blackmail, or engineered dependency.",
    "No pressure designed to bypass sexual, romantic, medical, legal, or financial consent.",
    "No exploitation of minors, impairment, crisis, severe distress, or other vulnerability.",
    "A refusal must remain safe and practically available.",
    "Behavioral cues are not a lie detector; important conclusions require corroboration.",
)


_DEFAULTS: dict[str, tuple[str, ...]] = {
    "self": ("open_questions_reflections", "autonomy_support", "micro_commitment"),
    "relationship": ("active_listening", "open_questions_reflections", "autonomy_support", "common_ground"),
    "sales": ("active_listening", "micro_commitment", "reciprocity", "truthful_social_proof", "legitimate_authority", "honest_scarcity"),
    "negotiation": ("active_listening", "open_questions_reflections", "autonomy_support", "context_framing"),
    "leadership": ("active_listening", "shared_identity", "legitimate_authority", "micro_commitment"),
    "service": ("active_listening", "open_questions_reflections", "reciprocity", "micro_commitment"),
    "general": ("active_listening", "open_questions_reflections", "autonomy_support"),
}


def get_technique(key: str) -> Technique:
    try:
        return TECHNIQUE_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"unknown influence technique: {key}") from exc


def build_plan(*, arena: str, objective: str, include_behavior_observation: bool = False) -> InfluencePlan:
    normalized = arena.strip().lower()
    if normalized not in ARENAS:
        raise ValueError(f"unknown influence arena: {arena}")
    if not objective.strip():
        raise ValueError("objective must not be empty")
    keys = list(_DEFAULTS[normalized])
    if include_behavior_observation and "behavior_baseline" not in keys:
        keys.append("behavior_baseline")
    return InfluencePlan(normalized, objective.strip(), tuple(keys), BASE_GUARDRAILS)


def record_application(
    connection: sqlite3.Connection,
    *,
    technique_key: str,
    arena: str,
    objective: str,
    outcome: str = "unknown",
    metadata: dict[str, Any] | None = None,
):
    """Record technique use without storing private conversation content."""
    technique = get_technique(technique_key)
    normalized = arena.strip().lower()
    if normalized not in ARENAS:
        raise ValueError(f"unknown influence arena: {arena}")
    if not objective.strip():
        raise ValueError("objective must not be empty")
    payload = {
        "technique": technique.key,
        "arena": normalized,
        "objective": objective.strip()[:200],
        "outcome": outcome.strip()[:80] or "unknown",
        "evidence_grade": technique.evidence_grade,
        "metadata": metadata or {},
    }
    return append_event(connection, "influence.technique_applied", payload)
