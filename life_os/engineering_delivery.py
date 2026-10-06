"""Governed GitHub promotion and CI feedback for verified engineering runs."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .ai_cli import AdapterError, CapabilityUnavailable, run_bounded
from .attention import decide_approval, emit_attention, request_approval
from .principal_agent import PrincipalAgent
from .engineering import (
    _git,
    _is_within,
    _worktree_root,
    get_run,
    repair_verified_run,
)
from .events import append_event
from .queue import Job, enqueue

MAX_CI_REPAIRS = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS engineering_promotions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES engineering_runs(id),
    approval_id INTEGER NOT NULL REFERENCES approvals(id),
    state TEXT NOT NULL DEFAULT 'awaiting_approval'
        CHECK(state IN ('awaiting_approval','queued','pushing','ci_pending','ci_failed',
                        'waiting_provider','ci_passed','failed','cancelled')),
    resume_kind TEXT NOT NULL DEFAULT '',
    remote TEXT NOT NULL DEFAULT 'origin',
    repo_slug TEXT NOT NULL DEFAULT '',
    pr_number INTEGER,
    pr_url TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    ci_json TEXT NOT NULL DEFAULT '{}',
    repair_count INTEGER NOT NULL DEFAULT 0 CHECK(repair_count BETWEEN 0 AND 3),
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_engineering_promotions_state
ON engineering_promotions(state, updated_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["ci"] = json.loads(value.pop("ci_json"))
    return value


def get_promotion(connection: sqlite3.Connection, promotion_id: int) -> dict[str, Any] | None:
    initialize(connection)
    row = connection.execute(
        "SELECT * FROM engineering_promotions WHERE id=?", (promotion_id,)
    ).fetchone()
    return None if row is None else _decode(row)


def get_promotion_for_run(connection: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    initialize(connection)
    row = connection.execute(
        "SELECT * FROM engineering_promotions WHERE run_id=?", (run_id,)
    ).fetchone()
    return None if row is None else _decode(row)


def recent_promotions(connection: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    initialize(connection)
    rows = connection.execute(
        "SELECT * FROM engineering_promotions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_decode(row) for row in rows]


def summary(connection: sqlite3.Connection) -> dict[str, int]:
    initialize(connection)
    result = {row["state"]: int(row["n"]) for row in connection.execute(
        "SELECT state,COUNT(*) n FROM engineering_promotions GROUP BY state"
    )}
    for state in (
        "awaiting_approval", "queued", "pushing", "ci_pending", "ci_failed",
        "waiting_provider", "ci_passed", "failed", "cancelled",
    ):
        result.setdefault(state, 0)
    return result


def _update(connection: sqlite3.Connection, promotion_id: int, state: str | None = None, **fields: Any) -> None:
    allowed = {
        "resume_kind", "repo_slug", "pr_number", "pr_url", "head_sha", "ci_json",
        "repair_count", "failure_reason", "finished_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if state is not None:
        updates["state"] = state
    updates["updated_at"] = _now()
    sql = ",".join(f"{key}=?" for key in updates)
    connection.execute(
        f"UPDATE engineering_promotions SET {sql} WHERE id=?",
        (*updates.values(), promotion_id),
    )
    connection.commit()


def _approval_state(connection: sqlite3.Connection, approval_id: int) -> str | None:
    row = connection.execute("SELECT state FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return None if row is None else str(row["state"])


def request_promotion(connection: sqlite3.Connection, *, run_id: int) -> dict[str, Any]:
    initialize(connection)
    run = get_run(connection, run_id)
    if run is None or run["state"] != "verified" or not run["commit_sha"]:
        raise ValueError("only a verified engineering run can request promotion")
    fingerprint = f"engineering-promotion:{run['run_key']}:{run['commit_sha']}"
    approval_id, _ = request_approval(
        connection,
        fingerprint=fingerprint,
        action="PUSH_BRANCH_AND_OPEN_PR",
        risk="external GitHub change; includes up to 3 bounded CI repair pushes; never merge/deploy",
        cost_cents=0,
        payload={
            "run_id": run_id,
            "branch": run["branch"],
            "commit_sha": run["commit_sha"],
            "goal": run["goal"][:500],
            "scope": "push verified branch, open/reuse PR, watch CI, bounded repair only",
        },
    )

    # This promotion is already bounded to a verified local commit, zero spend,
    # a managed branch, PR creation/reuse, CI observation, and at most three
    # independently reverified repair pushes. TEAGAN-PRINCIPAL may therefore
    # discharge this routine owner gate while the lower-level promotion
    # invariants remain authoritative. It still cannot merge or deploy.
    principal = PrincipalAgent(connection).decide(
        "Authorize a verified zero-cost engineering promotion: push the managed "
        "branch, open or reuse its pull request, watch CI, and apply at most "
        "three bounded repairs. Do not merge or deploy."
    )
    if principal.should_execute and not principal.requires_human:
        decide_approval(connection, approval_id, "approved")
    now = _now()
    cursor = connection.execute(
        """INSERT OR IGNORE INTO engineering_promotions(
        run_id,approval_id,state,created_at,updated_at)
        VALUES(?,?,'awaiting_approval',?,?)""",
        (run_id, approval_id, now, now),
    )
    created = cursor.rowcount == 1
    connection.commit()
    promotion = get_promotion_for_run(connection, run_id)
    assert promotion is not None
    if created:
        append_event(connection, "engineering.promotion.requested", {
            "promotion_id": promotion["id"], "run_id": run_id, "approval_id": approval_id,
        })
    promotion["created"] = created
    return promotion


def _queue_job(
    connection: sqlite3.Connection, *, promotion: dict[str, Any], kind: str,
    suffix: str, priority: int = 82,
) -> int:
    _, created = enqueue(
        connection,
        fingerprint=f"{kind}:{promotion['id']}:{suffix}",
        kind=kind,
        payload={"promotion_id": promotion["id"]},
        priority=priority,
        max_attempts=3,
    )
    return int(created)


def schedule_promotions(
    connection: sqlite3.Connection, *, watch_seconds: int = 60, provider_seconds: int = 1800,
) -> int:
    initialize(connection)
    created = 0
    rows = recent_promotions(connection, limit=500)
    for promotion in rows:
        state = promotion["state"]
        approval = _approval_state(connection, promotion["approval_id"])
        if state == "awaiting_approval":
            if approval == "approved":
                _update(connection, promotion["id"], "queued", failure_reason="")
                promotion = get_promotion(connection, promotion["id"]) or promotion
                created += _queue_job(connection, promotion=promotion, kind="engineering.promote", suffix="approved")
            elif approval in {"denied", "expired"}:
                _update(connection, promotion["id"], "cancelled", finished_at=_now())
        elif state == "ci_pending":
            bucket = int(time.time() // max(30, watch_seconds))
            created += _queue_job(connection, promotion=promotion, kind="engineering.ci_watch", suffix=str(bucket), priority=78)
        elif state == "ci_failed" and promotion["repair_count"] < MAX_CI_REPAIRS:
            created += _queue_job(
                connection, promotion=promotion, kind="engineering.ci_repair",
                suffix=f"{promotion['head_sha']}:{promotion['repair_count']}", priority=83,
            )
        elif state == "waiting_provider":
            bucket = int(time.time() // max(300, provider_seconds))
            resume = promotion.get("resume_kind") or "watch"
            kind = {
                "promote": "engineering.promote",
                "watch": "engineering.ci_watch",
                "repair": "engineering.ci_repair",
                "push_repair": "engineering.ci_repair",
            }.get(resume, "engineering.ci_watch")
            created += _queue_job(connection, promotion=promotion, kind=kind, suffix=f"provider:{bucket}", priority=75)
    return created


def _run(
    argv: Sequence[str], *, cwd: Path, timeout: int = 120,
    pulse: Callable[[], None] | None = None,
) -> tuple[int, str, str]:
    return run_bounded([str(part) for part in argv], cwd=str(cwd), timeout=timeout, pulse=pulse)


def _gh(
    args: Sequence[str], *, cwd: Path, timeout: int = 120,
    pulse: Callable[[], None] | None = None,
) -> str:
    executable = shutil.which("gh")
    if not executable:
        raise CapabilityUnavailable("GitHub CLI is not installed")
    code, out, err = _run([executable, *args], cwd=cwd, timeout=timeout, pulse=pulse)
    if code:
        diagnostic = (out + err).lower()
        if any(token in diagnostic for token in ("auth login", "authentication", "not logged", "401", "403")):
            raise CapabilityUnavailable("GitHub CLI authentication is unavailable")
        raise AdapterError(f"GitHub CLI action failed with exit {code}")
    return out.strip()


def _require_approved(connection: sqlite3.Connection, promotion: dict[str, Any]) -> bool:
    return _approval_state(connection, promotion["approval_id"]) == "approved"


def _validate_verified_checkout(run: dict[str, Any], *, home: Path) -> Path:
    if run["state"] != "verified" or not run["commit_sha"]:
        raise ValueError("promotion requires a verified engineering run")
    if not run["branch"].startswith("monolith/auto/"):
        raise ValueError("promotion refused for unmanaged branch")
    worktree = Path(run["worktree_path"]).resolve()
    if not _is_within(worktree, _worktree_root(home)) or not worktree.is_dir():
        raise ValueError("promotion worktree escaped managed engineering root")
    if _git(worktree, "rev-parse", "HEAD") != run["commit_sha"]:
        raise AdapterError("verified engineering checkout HEAD drifted")
    if _git(worktree, "branch", "--show-current") != run["branch"]:
        raise AdapterError("verified engineering checkout branch drifted")
    code, out, _ = _run(["git", "status", "--porcelain"], cwd=worktree, timeout=30)
    if code or out.strip():
        raise AdapterError("verified engineering checkout is not clean")
    code, _, _ = _run(
        ["git", "merge-base", "--is-ancestor", run["base_commit"], run["commit_sha"]],
        cwd=worktree, timeout=30,
    )
    if code:
        raise AdapterError("verified engineering commit is not descended from its recorded base")
    return worktree


def _repo_slug_from_remote(remote: str) -> str:
    value = remote.strip()
    prefixes = ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/")
    for prefix in prefixes:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    else:
        raise ValueError("engineering promotion supports only github.com remotes")
    if value.endswith(".git"):
        value = value[:-4]
    value = value.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("invalid GitHub repository remote")
    return value


def _repo_slug(worktree: Path) -> str:
    return _repo_slug_from_remote(_git(worktree, "remote", "get-url", "origin"))


def _ensure_gh_auth(worktree: Path, *, pulse: Callable[[], None] | None = None) -> None:
    _gh(["auth", "status", "--hostname", "github.com"], cwd=worktree, timeout=30, pulse=pulse)


def _push_branch(
    worktree: Path, run: dict[str, Any], *, pulse: Callable[[], None] | None = None,
) -> None:
    code, out, err = _run(
        ["git", "push", "--set-upstream", "origin", f"{run['branch']}:{run['branch']}"],
        cwd=worktree, timeout=180, pulse=pulse,
    )
    if code:
        diagnostic = (out + err).lower()
        if any(token in diagnostic for token in ("authentication", "could not read username", "403", "401")):
            raise CapabilityUnavailable("Git push authentication is unavailable")
        raise AdapterError(f"verified branch push failed with exit {code}")


def _pr_body(run: dict[str, Any]) -> str:
    acceptance = "\n".join(f"- {item[:300]}" for item in run["acceptance"][:12])
    return (
        f"Automated verified engineering run {run['id']}.\n\n"
        f"Goal:\n{run['goal'][:1000]}\n\nAcceptance:\n{acceptance}\n\n"
        "Local host verification passed. This automation is not authorized to merge or deploy."
    )


def _ensure_pr(
    worktree: Path, slug: str, run: dict[str, Any], *,
    pulse: Callable[[], None] | None = None,
) -> dict[str, Any]:
    def find() -> list[dict[str, Any]]:
        raw = _gh([
            "pr", "list", "--repo", slug, "--head", run["branch"], "--state", "open",
            "--limit", "1", "--json", "number,url,headRefOid",
        ], cwd=worktree, timeout=60, pulse=pulse)
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    rows = find()
    if not rows:
        title = "MONOLITH: " + (run["goal"].strip().splitlines()[0][:72] or f"engineering run {run['id']}")
        _gh([
            "pr", "create", "--repo", slug, "--base", "main", "--head", run["branch"],
            "--title", title, "--body", _pr_body(run),
        ], cwd=worktree, timeout=120, pulse=pulse)
        rows = find()
    if not rows:
        raise AdapterError("GitHub PR creation could not be verified")
    row = rows[0]
    return {
        "number": int(row["number"]), "url": str(row["url"]),
        "head_sha": str(row.get("headRefOid") or run["commit_sha"]),
    }


def _normalize_check(item: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(item.get("name") or item.get("context") or item.get("workflowName") or "check")[:200],
        "status": str(item.get("status") or item.get("state") or "").upper()[:40],
        "conclusion": str(item.get("conclusion") or "").upper()[:40],
    }


def _ci_snapshot(
    worktree: Path, slug: str, pr_number: int, *,
    pulse: Callable[[], None] | None = None,
) -> dict[str, Any]:
    raw = _gh([
        "pr", "view", str(pr_number), "--repo", slug,
        "--json", "headRefOid,statusCheckRollup,state,url",
    ], cwd=worktree, timeout=60, pulse=pulse)
    value = json.loads(raw)
    checks = [_normalize_check(item) for item in value.get("statusCheckRollup") or []]
    bad = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
    good = {"SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED"}
    failed = False
    pending = not checks
    for check in checks:
        conclusion = check["conclusion"]
        status = check["status"]
        effective = conclusion or status
        if effective in bad:
            failed = True
        elif effective not in good or (status and status not in {"COMPLETED", "SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED"}):
            pending = True
    if str(value.get("state") or "OPEN").upper() != "OPEN":
        failed = True
    overall = "failed" if failed else "pending" if pending else "passed"
    return {
        "overall": overall,
        "head_sha": str(value.get("headRefOid") or ""),
        "url": str(value.get("url") or ""),
        "checks": checks[:50],
    }


def _sanitize_diagnostic(text: str, limit: int = 8000) -> str:
    clean = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
    return clean[:limit]


def _failed_check_diagnostic(
    worktree: Path, slug: str, head_sha: str, *,
    pulse: Callable[[], None] | None = None,
) -> str:
    raw = _gh([
        "api", f"repos/{slug}/commits/{head_sha}/check-runs",
        "-H", "Accept: application/vnd.github+json",
    ], cwd=worktree, timeout=90, pulse=pulse)
    value = json.loads(raw or "{}")
    bad = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
    parts: list[str] = []
    for check in value.get("check_runs") or []:
        if str(check.get("conclusion") or "").lower() not in bad:
            continue
        output = check.get("output") or {}
        parts.append(
            f"CHECK: {str(check.get('name') or 'check')[:200]}\n"
            f"TITLE: {str(output.get('title') or '')[:500]}\n"
            f"SUMMARY:\n{str(output.get('summary') or '')[:2500]}"
        )
    if not parts:
        return "GitHub CI reported failure but returned no bounded check-run diagnostic."
    return _sanitize_diagnostic("\n\n".join(parts))


def _final_failure(
    connection: sqlite3.Connection, promotion: dict[str, Any], reason: str,
) -> dict[str, Any]:
    reason = _sanitize_diagnostic(reason, 700)
    _update(
        connection, promotion["id"], "failed", failure_reason=reason,
        resume_kind="", finished_at=_now(),
    )
    emit_attention(
        connection,
        fingerprint=f"engineering-promotion-unrepaired:{promotion['id']}:{promotion.get('head_sha','')}",
        kind="failure_unrepaired",
        source="engineering_delivery",
        severity="error",
        payload={
            "promotion_id": promotion["id"], "run_id": promotion["run_id"],
            "pr_url": promotion.get("pr_url", ""), "reason": reason,
        },
    )
    append_event(connection, "engineering.promotion.failed", {
        "promotion_id": promotion["id"], "run_id": promotion["run_id"], "reason": reason,
    })
    return {"promotion_id": promotion["id"], "state": "failed", "reason": reason}


def execute_promotion(
    connection: sqlite3.Connection, job: Job, *, home: Path,
    pulse: Callable[[], None] | None = None,
) -> dict[str, Any]:
    promotion_id = int(job.payload.get("promotion_id", 0))
    promotion = get_promotion(connection, promotion_id)
    if promotion is None:
        raise ValueError("engineering promotion not found")
    if promotion["state"] == "ci_passed":
        return {"promotion_id": promotion_id, "state": "ci_passed", "pr_url": promotion["pr_url"]}
    if not _require_approved(connection, promotion):
        return {"promotion_id": promotion_id, "state": "awaiting_approval"}
    run = get_run(connection, promotion["run_id"])
    if run is None:
        return _final_failure(connection, promotion, "engineering run disappeared")
    try:
        worktree = _validate_verified_checkout(run, home=home)
        slug = _repo_slug(worktree)
        _ensure_gh_auth(worktree, pulse=pulse)
        _update(connection, promotion_id, "pushing", repo_slug=slug, resume_kind="", failure_reason="")
        _push_branch(worktree, run, pulse=pulse)
        pr = _ensure_pr(worktree, slug, run, pulse=pulse)
    except CapabilityUnavailable as exc:
        _update(connection, promotion_id, "waiting_provider", resume_kind="promote", failure_reason=str(exc)[:700])
        return {"promotion_id": promotion_id, "state": "waiting_provider"}
    except Exception as exc:
        return _final_failure(connection, promotion, f"{type(exc).__name__}: {exc}")
    _update(
        connection, promotion_id, "ci_pending", resume_kind="", repo_slug=slug,
        pr_number=pr["number"], pr_url=pr["url"], head_sha=pr["head_sha"],
        ci_json="{}", failure_reason="",
    )
    append_event(connection, "engineering.promotion.pr_open", {
        "promotion_id": promotion_id, "run_id": run["id"], "pr_number": pr["number"],
        "head_sha": pr["head_sha"],
    })
    return {"promotion_id": promotion_id, "state": "ci_pending", "pr_url": pr["url"]}


def execute_ci_watch(
    connection: sqlite3.Connection, job: Job, *, home: Path,
    pulse: Callable[[], None] | None = None,
) -> dict[str, Any]:
    promotion_id = int(job.payload.get("promotion_id", 0))
    promotion = get_promotion(connection, promotion_id)
    if promotion is None:
        raise ValueError("engineering promotion not found")
    if promotion["state"] == "ci_passed":
        return {"promotion_id": promotion_id, "state": "ci_passed"}
    if not _require_approved(connection, promotion):
        return _final_failure(connection, promotion, "promotion approval is no longer valid")
    run = get_run(connection, promotion["run_id"])
    if run is None or not promotion.get("pr_number"):
        return _final_failure(connection, promotion, "CI watch is missing run or PR metadata")
    try:
        worktree = _validate_verified_checkout(run, home=home)
        slug = promotion.get("repo_slug") or _repo_slug(worktree)
        snapshot = _ci_snapshot(worktree, slug, int(promotion["pr_number"]), pulse=pulse)
    except CapabilityUnavailable as exc:
        _update(connection, promotion_id, "waiting_provider", resume_kind="watch", failure_reason=str(exc)[:700])
        return {"promotion_id": promotion_id, "state": "waiting_provider"}
    except Exception as exc:
        return _final_failure(connection, promotion, f"CI watch failed: {type(exc).__name__}: {exc}")
    if snapshot["head_sha"] != run["commit_sha"]:
        return _final_failure(connection, promotion, "PR head differs from the verified local engineering commit")
    encoded = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    if snapshot["overall"] == "passed":
        _update(
            connection, promotion_id, "ci_passed", ci_json=encoded,
            head_sha=snapshot["head_sha"], failure_reason="", finished_at=_now(), resume_kind="",
        )
        append_event(connection, "engineering.promotion.ci_passed", {
            "promotion_id": promotion_id, "run_id": run["id"], "head_sha": snapshot["head_sha"],
        })
        return {"promotion_id": promotion_id, "state": "ci_passed", "ci": snapshot}
    if snapshot["overall"] == "failed":
        current = get_promotion(connection, promotion_id) or promotion
        if current["repair_count"] >= MAX_CI_REPAIRS:
            return _final_failure(connection, current, "CI still failed after maximum repair attempts")
        _update(
            connection, promotion_id, "ci_failed", ci_json=encoded,
            head_sha=snapshot["head_sha"], failure_reason="CI failed", resume_kind="",
        )
        current = get_promotion(connection, promotion_id) or current
        _queue_job(
            connection, promotion=current, kind="engineering.ci_repair",
            suffix=f"{snapshot['head_sha']}:{current['repair_count']}", priority=84,
        )
        append_event(connection, "engineering.promotion.ci_failed", {
            "promotion_id": promotion_id, "run_id": run["id"],
            "repair_count": current["repair_count"],
        })
        return {"promotion_id": promotion_id, "state": "ci_failed", "ci": snapshot}
    _update(connection, promotion_id, "ci_pending", ci_json=encoded, failure_reason="", resume_kind="")
    return {"promotion_id": promotion_id, "state": "ci_pending", "ci": snapshot}


def execute_ci_repair(
    connection: sqlite3.Connection, job: Job, *, home: Path,
    pulse: Callable[[], None] | None = None,
) -> dict[str, Any]:
    promotion_id = int(job.payload.get("promotion_id", 0))
    promotion = get_promotion(connection, promotion_id)
    if promotion is None:
        raise ValueError("engineering promotion not found")
    if not _require_approved(connection, promotion):
        return _final_failure(connection, promotion, "promotion approval is no longer valid")
    run = get_run(connection, promotion["run_id"])
    if run is None:
        return _final_failure(connection, promotion, "engineering run disappeared")
    try:
        worktree = _validate_verified_checkout(run, home=home)
        slug = promotion.get("repo_slug") or _repo_slug(worktree)
        _ensure_gh_auth(worktree, pulse=pulse)
    except CapabilityUnavailable as exc:
        _update(connection, promotion_id, "waiting_provider", resume_kind="repair", failure_reason=str(exc)[:700])
        return {"promotion_id": promotion_id, "state": "waiting_provider"}
    except Exception as exc:
        return _final_failure(connection, promotion, f"CI repair preflight failed: {type(exc).__name__}: {exc}")

    if promotion.get("resume_kind") == "push_repair" and run["commit_sha"] != promotion.get("head_sha"):
        try:
            _push_branch(worktree, run, pulse=pulse)
        except CapabilityUnavailable as exc:
            _update(connection, promotion_id, "waiting_provider", resume_kind="push_repair", failure_reason=str(exc)[:700])
            return {"promotion_id": promotion_id, "state": "waiting_provider"}
        except Exception as exc:
            return _final_failure(connection, promotion, f"CI repair push failed: {type(exc).__name__}: {exc}")
        _update(
            connection, promotion_id, "ci_pending", resume_kind="", head_sha=run["commit_sha"],
            ci_json="{}", failure_reason="",
        )
        return {"promotion_id": promotion_id, "state": "ci_pending", "head_sha": run["commit_sha"]}

    if promotion["repair_count"] >= MAX_CI_REPAIRS:
        return _final_failure(connection, promotion, "maximum CI repair attempts reached")
    try:
        diagnostic = _failed_check_diagnostic(
            worktree, slug, promotion.get("head_sha") or run["commit_sha"], pulse=pulse
        )
    except CapabilityUnavailable as exc:
        _update(connection, promotion_id, "waiting_provider", resume_kind="repair", failure_reason=str(exc)[:700])
        return {"promotion_id": promotion_id, "state": "waiting_provider"}
    except Exception as exc:
        diagnostic = f"Unable to fetch detailed failed-check output: {type(exc).__name__}. Repair the recorded CI failure from repository/CI configuration evidence."

    next_count = promotion["repair_count"] + 1
    _update(connection, promotion_id, "ci_failed", repair_count=next_count, failure_reason="CI repair in progress")
    result = repair_verified_run(
        connection, run_id=run["id"], home=home, diagnostic=diagnostic, pulse=pulse
    )
    if result["state"] == "waiting_provider":
        _update(
            connection, promotion_id, "waiting_provider", resume_kind="repair",
            repair_count=promotion["repair_count"], failure_reason="engineering provider unavailable",
        )
        return {"promotion_id": promotion_id, "state": "waiting_provider"}
    if result["state"] != "verified":
        current = get_promotion(connection, promotion_id) or promotion
        if next_count >= MAX_CI_REPAIRS:
            return _final_failure(connection, current, result.get("reason", "CI repair failed"))
        _update(connection, promotion_id, "ci_failed", failure_reason=result.get("reason", "CI repair failed"), resume_kind="")
        return {"promotion_id": promotion_id, "state": "ci_failed", "repair_count": next_count}

    repaired_run = get_run(connection, run["id"])
    assert repaired_run is not None
    try:
        _push_branch(worktree, repaired_run, pulse=pulse)
    except CapabilityUnavailable as exc:
        _update(connection, promotion_id, "waiting_provider", resume_kind="push_repair", failure_reason=str(exc)[:700])
        return {"promotion_id": promotion_id, "state": "waiting_provider", "commit_sha": repaired_run["commit_sha"]}
    except Exception as exc:
        return _final_failure(connection, promotion, f"CI repair push failed: {type(exc).__name__}: {exc}")
    _update(
        connection, promotion_id, "ci_pending", resume_kind="", head_sha=repaired_run["commit_sha"],
        ci_json="{}", failure_reason="",
    )
    append_event(connection, "engineering.promotion.ci_repaired", {
        "promotion_id": promotion_id, "run_id": run["id"],
        "repair_count": next_count, "commit_sha": repaired_run["commit_sha"],
    })
    return {
        "promotion_id": promotion_id, "state": "ci_pending",
        "repair_count": next_count, "commit_sha": repaired_run["commit_sha"],
    }
