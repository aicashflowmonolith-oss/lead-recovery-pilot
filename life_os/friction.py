"""Observed owner effort, without interpreting missing coverage as zero effort."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from .events import append_event
from .queue import get_state, initialize_queue

BASELINE = "friction.observation_started_at"
MANUAL_ACTIONS = {"task_add", "purchase_add", "account_add", "transaction_add", "checkin_save",
                  "entity_add", "classify_unclassified", "known_review"}


def start_observing(c):
    initialize_queue(c)
    c.execute("INSERT OR IGNORE INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
              (BASELINE, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
    c.commit()


def record_owner_action(c, action):
    # Store the action category only; never command text, health data or note content.
    start_observing(c)
    if action == "quick_add":
        kind = "command"
    elif action in MANUAL_ACTIONS:
        kind = "manual_operation"
    elif action == "worker_resume":
        kind = "manual_resume"
    elif action in {"worker_pause", "worker_emergency_stop"}:
        kind = "owner_control"
    elif action == "approval_decide":
        kind = "authorization"
    elif action == "attention_ack":
        kind = "outcome_review"
    else:
        kind = "goal_or_preference"
    append_event(c, "friction." + kind, {"surface": "local_app", "action": action})


def snapshot(c, *, now=None):
    started = get_state(c, BASELINE)
    if started is None:
        return {"observing_since": None, "observed": {}, "previous_seven_days": None,
                "unmeasured": ["required prompts", "context re-entry", "unnecessary escalations", "research/checking minutes", "reminders outside this app"]}
    current = now or datetime.now(timezone.utc)
    def counts(start, end):
        rows = c.execute("SELECT kind,COUNT(*) AS n FROM events WHERE occurred_at>=? AND occurred_at<? AND kind LIKE 'friction.%' GROUP BY kind", (start, end))
        result = {name: 0 for name in ("command", "manual_operation", "manual_resume", "owner_control", "authorization", "outcome_review", "goal_or_preference")}
        result.update({r["kind"].removeprefix("friction."): r["n"] for r in rows})
        return result
    cutoff = (current - timedelta(days=7)).isoformat()
    previous = (current - timedelta(days=14)).isoformat()
    observed = counts(max(started, cutoff), (current + timedelta(microseconds=1)).isoformat())
    revenue = get_state(c, "revenue.last_followthrough")
    return {"observing_since": started, "window_days": 7, "observed": observed,
            "previous_seven_days": counts(previous, cutoff) if started <= previous else None,
            "revenue_followthrough": json.loads(revenue) if revenue else None,
            "machine_requests": c.execute("SELECT COUNT(*) FROM sync_outbox WHERE source='revenue-followthrough:' AND created_at>=?", (started,)).fetchone()[0],
            "assessments_completed": c.execute("SELECT COUNT(*) FROM sync_inbox WHERE kind='revenue.reply_assessment' AND source='revenue-connector' AND state='processed' AND processed_at>=?", (started,)).fetchone()[0],
            "unmeasured": ["required prompts", "context re-entry", "unnecessary escalations", "research/checking minutes", "reminders outside this app"]}
