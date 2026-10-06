"""Exact governed handoff lineage, derived without new objectives or models.

No text similarity, inferred acceptance coverage, or installed-completion claims.
Git identity checks run before the caller's short SQLite write transaction; the
entire authority/receipt/identity snapshot is checked again under that lock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .ai_cli import AdapterError
from .queue import get_state

POLICY_KEY = "engineering_handoff"
MAX_SOURCES = 100
IDENTITY_COLUMNS = "id,goal,acceptance_json,repo_root,base_commit"
OPEN_STATES = {"queued", "building", "verifying", "waiting_provider"}


def stopped(connection):
    return any(get_state(connection, key) == "1" for key in
               ("worker.paused", "worker.emergency_stop", "safe_mode.paused"))


def _git(repo, *args, pulse=None):
    from .engineering import _git as run_git
    return run_git(Path(repo), "-c", f"safe.directory={Path(repo).resolve()}",
                   *args, timeout=10, pulse=pulse)


def repository_identity(repo, *, pulse=None, _seen=None, cache=None):
    """Verify local Git identity; local clones inherit their source identity.

    Remote identity alone is insufficient for aliasing: same_repository also
    requires the recorded base to be present and ancestral in both checkouts.
    No remote command, auth change, or network call occurs here.
    """
    repo = Path(repo).resolve()
    cache = {} if cache is None else cache
    key = str(repo)
    if key in cache:
        if cache[key] is None:
            raise ValueError("repository identity is unavailable in this resolution")
        return cache[key]
    seen = set() if _seen is None else _seen
    if str(repo) in seen or len(seen) >= 4:
        raise ValueError("repository identity chain is not bounded")
    seen.add(str(repo))
    # Cache unavailable/cyclic identities too: 25 receipts for one broken
    # checkout must not spawn 25 identical failing probe trees on a 4 GB host.
    cache[key] = None
    common = Path(_git(repo, "rev-parse", "--git-common-dir", pulse=pulse))
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    from .engineering import _exec
    code, remote, _ = _exec(["git", "-c", f"safe.directory={repo}",
                             "remote", "get-url", "origin"], cwd=repo, timeout=10, pulse=pulse)
    if code in {1, 2}:
        cache[key] = "git:" + os.path.normcase(str(common))
        return cache[key]
    if code:
        raise AdapterError("repository origin cannot be verified")
    remote = remote.strip()
    if not remote:
        raise ValueError("empty repository origin")
    # Local source clones/worktrees prove identity through their actual Git
    # source, rather than pretending a configured URL is an authority grant.
    if remote.startswith("file://"):
        from urllib.request import url2pathname
        parsed = urlsplit(remote)
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("remote file origin is not a local identity proof")
        local = Path(url2pathname(parsed.path))
    elif "://" not in remote and not re.match(r"[^/@:]+@[^/:]+:", remote):
        local = Path(remote)
        local = repo / local if not local.is_absolute() else local
    else:
        local = None
    if local is not None:
        if not local.exists():
            # A detached runtime must still replay its own exact local rows if
            # a temporary clone source disappears. This proves no cross-clone
            # alias; only the surviving repository's common Git directory.
            cache[key] = "git:" + os.path.normcase(str(common))
            return cache[key]
        cache[key] = repository_identity(local, pulse=pulse, _seen=seen, cache=cache)
        return cache[key]
    # Normalize transport spelling only; no fuzzy repository/goal equivalence.
    match = re.fullmatch(r"git@([^/:]+):(.+)", remote)
    if match:
        host, path = match.groups()
    else:
        parsed = urlsplit(remote)
        if (parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.password
                or parsed.query or parsed.fragment
                or (parsed.scheme == "ssh" and parsed.username not in {None, "git"})):
            raise ValueError("repository origin identity is unsupported")
        if parsed.scheme == "https" and parsed.username:
            raise ValueError("credential-bearing origin is not an identity proof")
        host = parsed.hostname + (":" + str(parsed.port) if parsed.port else "")
        path = parsed.path.lstrip("/")
    if not path or ".." in path.split("/"):
        raise ValueError("repository origin identity is invalid")
    cache[key] = "origin:" + host.lower() + "/" + path.removesuffix(".git").rstrip("/")
    return cache[key]


def same_repository(left, right, base_commit, *, pulse=None, cache=None):
    cache = {} if cache is None else cache
    left, right = Path(left).resolve(), Path(right).resolve()
    try:
        for path in (left, right):
            key = str(path)
            if key not in cache:
                cache[key] = repository_identity(path, pulse=pulse, cache=cache)
            if cache[key] is None:
                return False
        if cache[str(left)] != cache[str(right)]:
            return False
        if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
            return False
        for path in {left, right}:
            check = (str(path), base_commit)
            if check not in cache:
                cache[check] = False
                _git(path, "merge-base", "--is-ancestor", base_commit, "HEAD", pulse=pulse)
                cache[check] = True
            if not cache[check]:
                return False
        return True
    except (AdapterError, OSError, ValueError, RuntimeError):
        return False


def _snapshot(connection):
    policy_row = connection.execute("SELECT value_json FROM brain_policy_state WHERE key=?", (POLICY_KEY,)).fetchone()
    if not policy_row:
        return None
    policy = json.loads(policy_row[0])
    if not isinstance(policy, dict):
        raise ValueError("handoff policy is not exact evidence")
    sources = policy.get("source_run_ids")
    if (not isinstance(sources, list) or len(sources) > MAX_SOURCES
            or any(type(item) is not int or not 0 < item < 2**63 for item in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("handoff source references are not bounded exact evidence")
    approval = connection.execute("SELECT action,state,cost_cents,payload_json,expires_at FROM approvals WHERE id=?",
                                  (policy.get("approval_id"),)).fetchone()
    raw = get_state(connection, "engineering.handoff.receipts:" + str(policy["revision"])) or "{}"
    receipts = json.loads(raw)
    if not isinstance(receipts, dict) or len(receipts) > MAX_SOURCES:
        raise ValueError("handoff receipts are not bounded exact evidence")
    ids = set(sources)
    ids.update(item.get("successor_run_id") for item in receipts.values() if isinstance(item, dict)
               and type(item.get("successor_run_id")) is int and 0 < item["successor_run_id"] < 2**63)
    rows = connection.execute("SELECT " + IDENTITY_COLUMNS + " FROM engineering_runs WHERE id IN ("
                              + ",".join("?" for _ in ids) + ") ORDER BY id", sorted(ids)).fetchall() if ids else []
    return {"policy": policy_row[0], "approval": tuple(approval) if approval else None, "receipts": raw,
            "rows": tuple(tuple(row) for row in rows)}


def _authorized(snapshot):
    policy, approval = json.loads(snapshot["policy"]), snapshot["approval"]
    if (policy.get("activation_state") != "active" or not approval
            or approval[0] != "ACTIVATE_ENGINEERING_HANDOFF" or approval[1] != "approved" or approval[2] != 0):
        return False
    try:
        payload = json.loads(approval[3])
    except (ValueError, TypeError):
        return False
    if (not isinstance(payload, dict) or payload.get("revision") != policy.get("revision")
            or payload.get("runtime_commit") != policy.get("runtime_commit")):
        return False
    if not approval[4]:
        return True
    try:
        expiry = datetime.fromisoformat(approval[4])
        return expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


@dataclass
class Lineage:
    snapshot: dict | None
    edges: dict[int, int] = field(default_factory=dict)
    blocked: set[int] = field(default_factory=set)
    approval_id: int | None = None
    repo_cache: dict = field(default_factory=dict)

    def current(self, connection):
        return _snapshot(connection) == self.snapshot and (self.snapshot is None or not self.edges or _authorized(self.snapshot))

    def root(self, run_id):
        visited = set()
        while run_id in self.edges:
            if run_id in visited:
                raise ValueError("cyclic handoff evidence cannot admit work")
            visited.add(run_id)
            run_id = self.edges[run_id]
        return run_id

    def family(self, run_id):
        return {run_id} | {source for source in self.edges if self.root(source) == run_id}


def qualified_lineage(connection, *, pulse=None, cache=None):
    snapshot = _snapshot(connection)
    view = Lineage(snapshot)
    if cache is not None:
        view.repo_cache = cache
    if snapshot is None:
        return view
    policy = json.loads(snapshot["policy"])
    view.approval_id = policy["approval_id"]
    sources = set(policy["source_run_ids"])
    rows = {row[0]: dict(zip(IDENTITY_COLUMNS.split(","), row)) for row in snapshot["rows"]}
    authorized = _authorized(snapshot)
    for key, receipt in json.loads(snapshot["receipts"]).items():
        if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key) or int(key) not in sources:
            continue
        source_id = int(key)
        successor_id = receipt.get("successor_run_id") if isinstance(receipt, dict) else None
        if not authorized:
            view.blocked.add(source_id)
            if type(successor_id) is int:
                view.blocked.add(successor_id)
            continue
        source, successor = rows.get(source_id), rows.get(successor_id)
        if (type(successor_id) is not int or successor_id == source_id or not source or not successor
                or source["goal"] != successor["goal"]
                or receipt.get("base_commit") != successor["base_commit"]
                or json.loads(successor["acceptance_json"]) != json.loads(source["acceptance_json"]) +
                [f"Recovery of engineering run {source_id}; handoff {policy['revision']}; preserve original evidence"]
                or not same_repository(source["repo_root"], successor["repo_root"], source["base_commit"],
                                       pulse=pulse, cache=view.repo_cache)):
            view.blocked.add(source_id)
            if type(successor_id) is int:
                view.blocked.add(successor_id)
            continue
        view.edges[source_id] = successor_id
    for source in list(view.edges):
        try:
            view.root(source)
        except ValueError:
            view.blocked.add(source)
    for source in view.blocked:
        view.edges.pop(source, None)
    return view


def qualified_recovery_request(view, source_id, goal, acceptance, repo, *, pulse=None):
    """Validate the existing handoff's crash-before-receipt request, not a link.

    The existing reconciler may restore its receipt after reusing that exact
    already-created successor. This function never creates or writes lineage.
    """
    if view.snapshot is None or not _authorized(view.snapshot) or type(source_id) is not int:
        return False
    policy = json.loads(view.snapshot["policy"])
    if source_id not in policy["source_run_ids"] or source_id in view.blocked:
        return False
    source = next((dict(zip(IDENTITY_COLUMNS.split(","), row)) for row in view.snapshot["rows"]
                   if row[0] == source_id), None)
    return bool(source and source["goal"] == goal and acceptance == json.loads(source["acceptance_json"]) +
                [f"Recovery of engineering run {source_id}; handoff {policy['revision']}; preserve original evidence"]
                and same_repository(repo, source["repo_root"], source["base_commit"],
                                    pulse=pulse, cache=view.repo_cache))


def suppress_predecessor_jobs(connection, view, *, history=None):
    """Caller holds BEGIN IMMEDIATE; cancel obsolete attempts, never finish goals."""
    from .queue import _event, _iso
    history = admission_history(connection, view) if history is None else history
    # Use the ready-state index once. Pending jobs can change while repository
    # checks run, so read this bounded working set under the admission lock.
    pending = connection.execute("SELECT id,priority,payload_json FROM worker_jobs "
                                 "WHERE state IN ('queued','retry') AND kind='engineering.build'").fetchall()
    jobs_by_run = {}
    for job in pending:
        jobs_by_run.setdefault(json.loads(job["payload_json"]).get("run_id"), []).append(job)
    for source in view.edges:
        root = view.root(source)
        if root in view.blocked:
            continue
        for job in jobs_by_run.get(source, []):
            connection.execute("UPDATE worker_jobs SET state='cancelled',updated_at=? "
                               "WHERE id=? AND state IN ('queued','retry')", (_iso(), job[0]))
            _event(connection, job[0], "engineering.lineage.superseded", {
                "source_run_id": source, "successor_run_id": root,
                "approval_id": view.approval_id, "completed_work": False,
            })
    for root in {view.root(source) for source in view.edges} - view.blocked:
        priority, ready_at = family_priority_age(history, view, root)
        for job in jobs_by_run.get(root, []):
            payload = json.loads(job["payload_json"])
            updated = {**payload, "original_ready_at": ready_at,
                       "lineage_source_run_ids": sorted(view.family(root))}
            if updated != payload or priority > job["priority"]:
                connection.execute("UPDATE worker_jobs SET priority=max(priority,?),payload_json=?,updated_at=? "
                                   "WHERE id=? AND state IN ('queued','retry')",
                                   (priority, json.dumps(updated, separators=(",", ":"), sort_keys=True), _iso(), job["id"]))
                _event(connection, job["id"], "engineering.lineage.inherited", {
                    "approval_id": view.approval_id, "source_run_ids": sorted(view.family(root)),
                    "previous_priority": job["priority"], "priority": max(priority, job["priority"]),
                    "original_ready_at": ready_at, "preserved_available_at": True})


def admission_history(connection, view):
    """One indexed aggregate for all admitted families, never N queue scans."""
    rows = connection.execute("SELECT id,created_at FROM engineering_runs").fetchall()
    history = {row["id"]: (70, row["created_at"]) for row in rows}
    if not history:
        return history
    jobs = connection.execute("SELECT json_extract(payload_json,'$.run_id') run_id,"
                              "MAX(priority) priority,"
                              "MIN(COALESCE(json_extract(payload_json,'$.original_ready_at'),available_at)) ready_at FROM worker_jobs "
                              "INDEXED BY idx_engineering_job_lineage WHERE kind='engineering.build' "
                              "GROUP BY json_extract(payload_json,'$.run_id')")
    for job in jobs:
        if job["run_id"] not in history:
            continue
        priority, _ = history[job["run_id"]]
        # A delayed job was not ready when its objective was first requested.
        # Use real queue evidence when present; creation is only a no-job fallback.
        history[job["run_id"]] = (max(priority, job["priority"]), job["ready_at"])
    return history


def family_priority_age(history, view, run_id):
    values = [history[item] for item in view.family(run_id)]
    return max(item[0] for item in values), min(item[1] for item in values)
