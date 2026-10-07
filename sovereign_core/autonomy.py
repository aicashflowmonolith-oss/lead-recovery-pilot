from __future__ import annotations

import sqlite3
from typing import Any

import monolith as core

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_goals_active
ON goals(status, priority, id);

CREATE TABLE IF NOT EXISTS goal_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    task_id INTEGER,
    result TEXT,
    last_error TEXT,
    updated_at REAL NOT NULL,
    UNIQUE(goal_id, position),
    FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_goal_steps
ON goal_steps(goal_id, position, status);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    interval_seconds REAL NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_due
ON schedules(enabled, next_run);
"""


def init_autonomy(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def create_goal(
    conn: sqlite3.Connection,
    title: str,
    objective: str,
    steps: list[dict[str, Any]],
    priority: int = 0,
) -> dict[str, Any]:
    init_autonomy(conn)
    title = title.strip()
    objective = objective.strip()
    if not title or not objective:
        raise ValueError("goal title and objective are required")
    if not steps:
        raise ValueError("goal requires at least one executable step")

    normalized: list[tuple[str, dict[str, Any]]] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("each goal step must be an object")
        kind = str(step.get("kind", "")).strip()
        payload = step.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("goal step payload must be an object")
        core.get_adapter(conn, kind)
        normalized.append((kind, payload))

    ts = core.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO goals(title,objective,status,priority,created_at,updated_at) VALUES(?,?,'active',?,?,?)",
            (title, objective, int(priority), ts, ts),
        )
        goal_id = int(cur.lastrowid)
        for position, (kind, payload) in enumerate(normalized):
            conn.execute(
                """INSERT INTO goal_steps(goal_id,position,kind,payload,status,updated_at)
                   VALUES(?,?,?,?, 'pending', ?)""",
                (goal_id, position, kind, core.dumps(payload), ts),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    core.audit(
        conn,
        "autonomy",
        "goal.create",
        "goal",
        goal_id,
        {"title": title, "objective": objective, "priority": int(priority), "steps": len(normalized)},
    )
    core.emit(conn, "goal.created", {"goal_id": goal_id, "title": title})
    return get_goal(conn, goal_id)


def get_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    init_autonomy(conn)
    goal = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if goal is None:
        raise ValueError(f"goal not found: {goal_id}")
    steps = conn.execute(
        "SELECT * FROM goal_steps WHERE goal_id=? ORDER BY position,id",
        (goal_id,),
    ).fetchall()
    item = dict(goal)
    item["steps"] = []
    for row in steps:
        step = dict(row)
        step["payload"] = core.loads(step["payload"], {})
        step["result"] = core.loads(step["result"], None)
        item["steps"].append(step)
    return item


def list_goals(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    init_autonomy(conn)
    rows = conn.execute(
        "SELECT id FROM goals ORDER BY priority DESC,id DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    return [get_goal(conn, int(row["id"])) for row in rows]


def add_schedule(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
    payload: dict[str, Any],
    interval_seconds: float,
    priority: int = 0,
    next_run: float | None = None,
) -> dict[str, Any]:
    init_autonomy(conn)
    name = name.strip()
    if not name:
        raise ValueError("schedule name is required")
    if not isinstance(payload, dict):
        raise ValueError("schedule payload must be an object")
    core.get_adapter(conn, kind)
    interval_seconds = float(interval_seconds)
    if interval_seconds < 60:
        raise ValueError("minimum schedule interval is 60 seconds")
    ts = core.now()
    run_at = ts if next_run is None else float(next_run)
    conn.execute(
        """INSERT INTO schedules(name,kind,payload,interval_seconds,priority,enabled,next_run,created_at,updated_at)
           VALUES(?,?,?,?,?,1,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             kind=excluded.kind,
             payload=excluded.payload,
             interval_seconds=excluded.interval_seconds,
             priority=excluded.priority,
             enabled=1,
             next_run=excluded.next_run,
             updated_at=excluded.updated_at""",
        (name, kind, core.dumps(payload), interval_seconds, int(priority), run_at, ts, ts),
    )
    row = conn.execute("SELECT * FROM schedules WHERE name=?", (name,)).fetchone()
    assert row is not None
    core.audit(
        conn,
        "autonomy",
        "schedule.upsert",
        "schedule",
        row["id"],
        {"name": name, "kind": kind, "interval_seconds": interval_seconds},
    )
    core.emit(conn, "schedule.updated", {"schedule_id": row["id"], "name": name})
    return schedule_to_dict(row)


def schedule_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = core.loads(item["payload"], {})
    item["enabled"] = bool(item["enabled"])
    return item


def list_schedules(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    init_autonomy(conn)
    rows = conn.execute(
        "SELECT * FROM schedules ORDER BY enabled DESC,next_run ASC,id ASC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    return [schedule_to_dict(row) for row in rows]


def run_due_schedules(conn: sqlite3.Connection) -> int:
    init_autonomy(conn)
    ts = core.now()
    rows = conn.execute(
        """SELECT * FROM schedules
           WHERE enabled=1 AND next_run<=?
           ORDER BY next_run ASC,id ASC""",
        (ts,),
    ).fetchall()
    submitted = 0
    for row in rows:
        payload = core.loads(row["payload"], {})
        task = core.submit_task(
            conn,
            str(row["kind"]),
            payload,
            priority=int(row["priority"]),
            actor="scheduler",
        )
        next_run = max(ts, float(row["next_run"])) + float(row["interval_seconds"])
        conn.execute(
            "UPDATE schedules SET next_run=?,updated_at=? WHERE id=?",
            (next_run, core.now(), row["id"]),
        )
        core.emit(
            conn,
            "schedule.fired",
            {"schedule_id": row["id"], "task_id": task["id"], "next_run": next_run},
        )
        submitted += 1
    return submitted


def advance_goals(conn: sqlite3.Connection) -> dict[str, int]:
    init_autonomy(conn)
    stats = {"submitted": 0, "completed": 0, "blocked": 0}
    goals = conn.execute(
        "SELECT * FROM goals WHERE status='active' ORDER BY priority DESC,id ASC"
    ).fetchall()

    for goal in goals:
        goal_id = int(goal["id"])
        while True:
            step = conn.execute(
                """SELECT * FROM goal_steps
                   WHERE goal_id=? AND status!='succeeded'
                   ORDER BY position,id LIMIT 1""",
                (goal_id,),
            ).fetchone()

            if step is None:
                conn.execute(
                    "UPDATE goals SET status='completed',updated_at=? WHERE id=?",
                    (core.now(), goal_id),
                )
                core.audit(conn, "autonomy", "goal.complete", "goal", goal_id, {})
                core.emit(conn, "goal.completed", {"goal_id": goal_id})
                stats["completed"] += 1
                break

            status = str(step["status"])
            if status == "pending":
                task = core.submit_task(
                    conn,
                    str(step["kind"]),
                    core.loads(step["payload"], {}),
                    priority=int(goal["priority"]),
                    actor="goal-engine",
                )
                conn.execute(
                    "UPDATE goal_steps SET status='running',task_id=?,updated_at=? WHERE id=?",
                    (task["id"], core.now(), step["id"]),
                )
                core.emit(
                    conn,
                    "goal.step.submitted",
                    {"goal_id": goal_id, "step_id": step["id"], "task_id": task["id"]},
                )
                stats["submitted"] += 1
                break

            if status == "running":
                task = conn.execute("SELECT * FROM tasks WHERE id=?", (step["task_id"],)).fetchone()
                if task is None:
                    conn.execute(
                        "UPDATE goal_steps SET status='failed',last_error='task missing',updated_at=? WHERE id=?",
                        (core.now(), step["id"]),
                    )
                    conn.execute(
                        "UPDATE goals SET status='blocked',updated_at=? WHERE id=?",
                        (core.now(), goal_id),
                    )
                    stats["blocked"] += 1
                    break

                task_status = str(task["status"])
                if task_status == "succeeded":
                    conn.execute(
                        """UPDATE goal_steps
                           SET status='succeeded',result=?,last_error=NULL,updated_at=?
                           WHERE id=?""",
                        (task["result"], core.now(), step["id"]),
                    )
                    core.emit(
                        conn,
                        "goal.step.succeeded",
                        {"goal_id": goal_id, "step_id": step["id"], "task_id": task["id"]},
                    )
                    continue

                if task_status in {"failed", "rejected"}:
                    error = task["last_error"] or task_status
                    conn.execute(
                        "UPDATE goal_steps SET status='failed',last_error=?,updated_at=? WHERE id=?",
                        (error, core.now(), step["id"]),
                    )
                    conn.execute(
                        "UPDATE goals SET status='blocked',updated_at=? WHERE id=?",
                        (core.now(), goal_id),
                    )
                    core.audit(
                        conn,
                        "autonomy",
                        "goal.block",
                        "goal",
                        goal_id,
                        {"step_id": step["id"], "task_id": task["id"], "error": error},
                    )
                    core.emit(
                        conn,
                        "goal.blocked",
                        {"goal_id": goal_id, "step_id": step["id"], "error": error},
                    )
                    stats["blocked"] += 1
                break

            conn.execute(
                "UPDATE goals SET status='blocked',updated_at=? WHERE id=?",
                (core.now(), goal_id),
            )
            stats["blocked"] += 1
            break

    return stats


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    init_autonomy(conn)
    goals = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status,COUNT(*) AS n FROM goals GROUP BY status")
    }
    return {
        "goals": goals,
        "schedules_enabled": conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE enabled=1"
        ).fetchone()[0],
        "next_schedule": (
            conn.execute("SELECT MIN(next_run) FROM schedules WHERE enabled=1").fetchone()[0]
        ),
    }


def tick(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "schedules_submitted": run_due_schedules(conn),
        "goals": advance_goals(conn),
    }
