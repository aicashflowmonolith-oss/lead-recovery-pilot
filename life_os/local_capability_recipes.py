"""Fixed local repair recipes; candidate text can never select code or commands.

The artifact writer is already an authorized request operation. Qualifying its
registry route grants only that same bounded operation, under its existing
request directory. This is not authority to install arbitrary capabilities.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile

from .ai_cli import CapabilityUnavailable, run_bounded
from .capabilities import set_capability_health, upsert_capability
from .events import append_event

ARTIFACT_CAPABILITY = "local.artifact.write"
RECIPE = "artifact-write-v1"
ADAPTER = "life_os.local_capability_recipes.v1"
QUALIFICATION_TIMEOUT_SECONDS = 15


class LocalCapabilityUnavailable(CapabilityUnavailable):
    capability = ARTIFACT_CAPABILITY


class RecipeAuthorityBlocked(ValueError):
    """An explicit registry gate cannot be cleared by a local repair."""


def _handler_digest():
    root = Path(__file__).parent
    return hashlib.sha256(b"".join(path.name.encode() + path.read_bytes() for path in
                                  (root / "request_fabric.py", Path(__file__)))).hexdigest()


def _row(c):
    row = c.execute("SELECT * FROM capabilities WHERE name=?", (ARTIFACT_CAPABILITY,)).fetchone()
    return None if row is None else dict(row)


def _gate(row):
    if row is None:
        return
    try:
        metadata = json.loads(row["metadata_json"])
        actions = json.loads(row["actions_json"])
        permissions = json.loads(row["permissions_json"])
    except (ValueError, TypeError):
        raise RecipeAuthorityBlocked("Existing capability ownership cannot be established") from None
    if not isinstance(metadata, dict):
        raise RecipeAuthorityBlocked("Existing capability ownership cannot be established")
    if (not row["enabled"] or row["auth_required"] or row["auth_status"] not in {"not_required", "ready"}
            or row["owner_approval_required"]
            or row["cost_fixed_cents"] != 0 or not row["reversible"]):
        raise RecipeAuthorityBlocked("Existing local capability authority gate retained")
    if (row["kind"] != "deterministic" or row["privacy_class"] != "internal"
            or actions != ["artifact.write"] or permissions != ["request.artifacts"]
            or metadata.get("adapter") != ADAPTER or metadata.get("recipe") != RECIPE):
        raise RecipeAuthorityBlocked("Existing capability is not owned by this fixed local recipe")


def repair(c, capability, *, home, pulse=None):
    if capability != ARTIFACT_CAPABILITY:
        return None
    _gate(_row(c))
    if pulse:
        pulse()
    expected = Path(home).resolve() / "execution" / "qualification"
    directory = expected.resolve()
    if directory != expected:
        raise ValueError("Local qualification directory escaped its authorized request home")
    directory.mkdir(parents=True, exist_ok=True)
    code, out, _err = run_bounded(
        [sys.executable, "-B", "-m", "life_os.local_capability_recipes",
         "--self-test", str(directory)],
        cwd=Path(__file__).resolve().parents[1], timeout=QUALIFICATION_TIMEOUT_SECONDS, pulse=pulse,
    )
    if code:
        raise ValueError("Fixed local recipe did not pass qualification")
    receipt = json.loads(out)
    if (not isinstance(receipt, dict) or set(receipt) != {"recipe", "passed", "assertions", "handler_sha256"}
            or receipt["recipe"] != RECIPE or receipt["passed"] is not True
            or receipt["assertions"] != 5 or receipt["handler_sha256"] != _handler_digest()):
        raise ValueError("Fixed local recipe returned an invalid qualification receipt")
    if pulse:
        pulse()
    # Recheck under the write lock: qualification must not race a newly applied
    # disabled, cost, credential or owner gate and overwrite that decision.
    c.execute("BEGIN IMMEDIATE")
    try:
        current = _row(c)
        _gate(current)
        metadata = json.loads(current["metadata_json"]) if current else {}
        metadata.update(adapter=ADAPTER, recipe=RECIPE, qualification=receipt)
        metadata["circuit_breaker"] = {"state": "closed", "consecutive_failures": 0,
                                       "reopen_at": None, "last_error": ""}
        if current:
            set_capability_health(c, ARTIFACT_CAPABILITY, "healthy", metadata)
        else:
            upsert_capability(c, name=ARTIFACT_CAPABILITY, kind="deterministic", health="healthy",
                              permissions=["request.artifacts"], actions=["artifact.write"],
                              auth_status="not_required", privacy_class="internal", cost_fixed_cents=0,
                              reversible=True, owner_approval_required=False, reliability=1.0,
                              recovery_method=RECIPE, metadata=metadata)
    except BaseException:
        c.rollback()
        raise
    append_event(c, "capability.local_recipe.qualified", {
        "name": ARTIFACT_CAPABILITY, "recipe": RECIPE, "qualification": receipt,
        "authority": "existing artifact.write operation allowlist", "candidate_code_executed": False,
    })
    return receipt


def require_artifact_writer(c, *, home, pulse=None):
    row = _row(c)
    try:
        # Cold discovery of an installed fixed primitive requires no provider.
        if row is None:
            repair(c, ARTIFACT_CAPABILITY, home=home, pulse=pulse)
            row = _row(c)
        _gate(row)
        metadata = json.loads(row["metadata_json"])
        qualification = metadata.get("qualification", {})
        circuit = metadata.get("circuit_breaker", {})
        if not isinstance(qualification, dict) or not isinstance(circuit, dict):
            raise ValueError("Local qualification metadata needs a fixed repair")
        if (row["health"] != "healthy" or circuit.get("state") != "closed"
                or set(qualification) != {"recipe", "passed", "assertions", "handler_sha256"}
                or qualification.get("assertions") != 5
                or qualification.get("passed") is not True
                or qualification.get("recipe") != RECIPE
                or qualification.get("handler_sha256") != _handler_digest()):
            raise ValueError("Local artifact route needs fixed qualification")
    except (RecipeAuthorityBlocked, ValueError, OSError, TimeoutError) as exc:
        raise LocalCapabilityUnavailable("Local artifact route unavailable: " + type(exc).__name__) from None


def _self_test(directory):
    from .request_fabric import validate_artifact, write_artifact
    # Only this fixed fixture is written. User candidate source is never run.
    with tempfile.TemporaryDirectory(prefix="artifact-recipe-", dir=directory) as temporary:
        home = Path(temporary).resolve()
        data = {"filename": "probe.json", "content": '{"qualification":true}'}
        _result, evidence = write_artifact(data, home=home, rid="0" * 32, ordinal=0)
        path = Path(evidence["path"])
        if not path.is_relative_to(home / "execution" / "artifacts") or path.read_text() != data["content"]:
            raise ValueError("Read-back qualification failed")
        if evidence["sha256"] != hashlib.sha256(data["content"].encode()).hexdigest():
            raise ValueError("Digest qualification failed")
        if write_artifact(data, home=home, rid="0" * 32, ordinal=0)[1] != evidence:
            raise ValueError("Replay qualification failed")
        for rejected in ({**data, "filename": "../outside.json"}, {**data, "content": '{"qualification":false}'}):
            try:
                if rejected["filename"] != data["filename"]:
                    validate_artifact(rejected)
                else:
                    write_artifact(rejected, home=home, rid="0" * 32, ordinal=0)
            except ValueError:
                continue
            raise ValueError("Containment qualification failed")
    return {"recipe": RECIPE, "passed": True, "assertions": 5, "handler_sha256": _handler_digest()}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--self-test":
        raise SystemExit("Only the fixed local self-test is supported")
    print(json.dumps(_self_test(Path(sys.argv[2]).resolve()), sort_keys=True))
