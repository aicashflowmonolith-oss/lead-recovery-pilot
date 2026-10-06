"""Canonical LIFE OS domain ontology.

The ontology is intentionally open: named domains cover known life areas while
the unclassified domain preserves signals that do not fit the current taxonomy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainSpec:
    key: str
    title: str
    description: str


DOMAIN_SPECS = (
    DomainSpec("health", "Health & Human Performance", "Whole-body health, care, performance, prevention, and exposome."),
    DomainSpec("money", "Money & Personal Finance", "Cash flow, banking, debt, savings, investing, benefits, taxes, and insurance costs."),
    DomainSpec("business", "Business, Income & Wealth Creation", "Business creation, sales, operations, revenue, profit, and scalable value creation."),
    DomainSpec("work", "Work, Career & Vocation", "Employment, freelancing, career capital, applications, credentials, and vocational transitions."),
    DomainSpec("goals", "Goals, Planning & Execution", "Goals, projects, tasks, milestones, dependencies, blockers, and adaptive planning."),
    DomainSpec("time", "Time, Calendar & Routines", "Calendar, routines, deadlines, habits, recurring maintenance, and time allocation."),
    DomainSpec("learning", "Learning, Knowledge & Second Brain", "Research, notes, knowledge, skills, procedures, provenance, and mastery."),
    DomainSpec("ai_automation", "AI, Automation & MONOLITH", "Model-agnostic agents, orchestration, tools, execution, verification, and automation."),
    DomainSpec("digital_life", "Digital Life, Devices & Computing", "Devices, software, networks, maintenance, compatibility, performance, and upgrades."),
    DomainSpec("security_privacy_identity", "Cybersecurity, Privacy & Identity", "Identity, authentication, permissions, privacy, threat defense, and account recovery."),
    DomainSpec("data_archive", "Data, Files, Backups & Archive", "Files, storage, versioning, backups, restore verification, and portability."),
    DomainSpec("home", "Home & Living Environment", "Housing, utilities, maintenance, household inventory, safety, and environment."),
    DomainSpec("food", "Food, Cooking & Groceries", "Food inventory, meal planning, cooking, nutrition logistics, cost, and food safety."),
    DomainSpec("procurement", "Purchases & Procurement", "Specifications, purchase queue, prices, quality, total cost, warranties, and acquisition."),
    DomainSpec("transportation", "Transportation, Mobility & Travel", "Licensing, vehicles, transit, mobility, routes, travel, bookings, and logistics."),
    DomainSpec("legal_admin", "Legal, Administrative & Government", "IDs, permits, benefits, contracts, government obligations, renewals, and records."),
    DomainSpec("relationships", "Relationships & Social Life", "Family, friends, romantic relationships, communities, commitments, and social support."),
    DomainSpec("communications", "Communications", "Email, messaging, calls, social channels, commitments, follow-ups, and contact context."),
    DomainSpec("personal_development", "Personal Development & Psychology", "Values, behavior change, reflection, attention, stress, and decision patterns."),
    DomainSpec("appearance", "Appearance, Grooming & Style", "Hair, skin, hygiene, oral care, fragrance, presentation, and grooming routines."),
    DomainSpec("wardrobe", "Clothing & Wardrobe", "Clothing, footwear, sizing, fit, care, seasonal needs, and replacement."),
    DomainSpec("recreation", "Recreation, Fun & Hobbies", "Leisure, hobbies, entertainment, creative activity, equipment, and downtime."),
    DomainSpec("fitness_sports", "Fitness, Sports & Outdoor Activity", "Training, sport, outdoor activity, performance, equipment, progression, and safety."),
    DomainSpec("media", "Media & Information Diet", "News, social media, music, video, reading, subscriptions, source quality, and attention."),
    DomainSpec("education", "Education & Formal Credentials", "School, courses, certifications, admissions, assignments, exams, and transcripts."),
    DomainSpec("community_civic", "Community, Civic & Volunteering", "Community services, volunteering, civic obligations, and local participation."),
    DomainSpec("pets", "Pets & Animals", "Animal health, veterinary care, supplies, training, licences, and household integration."),
    DomainSpec("safety_resilience", "Safety, Emergency & Resilience", "Emergency preparedness, continuity, recovery, first aid, and single-point-of-failure control."),
    DomainSpec("assets", "Assets, Inventory & Ownership", "Physical and digital assets, condition, location, maintenance, warranty, and resale value."),
    DomainSpec("documents", "Documents & Records", "Identity, medical, financial, legal, tax, certificates, correspondence, retention, and expiry."),
    DomainSpec("insurance", "Insurance & Risk Transfer", "Coverage, exclusions, premiums, deductibles, claims, renewals, and retained risk."),
    DomainSpec("future_design", "Future & Long-Term Life Design", "Multi-year choices, optionality, independence, housing, family, retirement, and scenarios."),
    DomainSpec("estate_legacy", "Estate & Digital Legacy", "Beneficiaries, directives, succession, digital assets, and emergency access."),
    DomainSpec("social_environment", "Social & Environmental Context", "Housing stability, access, infrastructure, climate, services, work conditions, and determinants."),
    DomainSpec("personal_policies", "Personal Policies & Decision Rules", "Versioned rules for spending, risk, approvals, privacy, automation, and escalation."),
    DomainSpec("metrics_reviews", "Metrics, Scorecards & Reviews", "Useful KPIs, reviews, trends, anomalies, unresolved risks, and outcome tracking."),
    DomainSpec("decision_engine", "Opportunity, Risk & Decision Engine", "Options, evidence, assumptions, reversibility, uncertainty, pre-mortems, and outcomes."),
    DomainSpec("dependencies_vendors", "Dependencies, Services & Vendors", "External services, costs, permissions, failure modes, alternatives, and concentration risk."),
    DomainSpec("location_context", "Location & Local Context", "Location-dependent services, laws, prices, transport, weather, and opportunities."),
    DomainSpec("life_events", "Life Events & Transitions", "Major changes that trigger cross-domain replanning and assumption updates."),
    DomainSpec("unclassified", "Unclassified / Novel Signals", "Unknown or novel items retained until they can be understood or classified."),
)

DOMAIN_REGISTRY = {spec.key: spec for spec in DOMAIN_SPECS}

DOMAIN_ALIASES = {
    "fitness": "fitness_sports",
    "nutrition": "food",
    "sleep": "health",
    "transport": "transportation",
    "privacy": "security_privacy_identity",
    "resilience": "safety_resilience",
    "life": "goals",
}


def normalize_domain(key: str | None) -> str:
    if not key:
        return "unclassified"
    value = key.strip().lower()
    value = DOMAIN_ALIASES.get(value, value)
    return value if value in DOMAIN_REGISTRY else "unclassified"


def require_domain(key: str) -> str:
    normalized = normalize_domain(key)
    if normalized == "unclassified" and key.strip().lower() != "unclassified":
        raise ValueError(f"unknown LIFE OS domain: {key}")
    return normalized
