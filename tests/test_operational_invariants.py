import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from life_os import autonomy_maintenance, operational_invariants
from life_os.db import connect, initialize
from life_os.queue import get_state


class OperationalInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.connection = connect(self.home / "life.db")
        initialize(self.connection)
        self.addCleanup(self.connection.close)

    def healthy(self):
        return {"healthy": True, "reason": "", "code_fixable": False}

    def test_provider_continuity_forces_one_bounded_reprobe(self):
        with patch.object(operational_invariants, "_ready_engineering_providers",
                          side_effect=[[], ["ollama"]]), \
             patch("life_os.engineering_providers.probe") as probe:
            result = operational_invariants._provider_invariant(self.connection, self.home)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["ready_providers"], ["ollama"])
        self.assertTrue(result["recovery_attempted"])
        probe.assert_called_once_with(self.connection, self.home, force=True)

    def test_native_control_missing_is_required_invariant(self):
        with patch.object(operational_invariants, "_provider_invariant", return_value=self.healthy()), \
             patch.object(operational_invariants, "_native_control_state", return_value={
                 "healthy": False, "status": "missing", "age_seconds": None,
                 "reason": "native control missing", "code_fixable": True,
             }), \
             patch.object(operational_invariants, "_delivery_invariant", return_value=self.healthy()), \
             patch.object(operational_invariants, "_submit_self_repair", return_value=None):
            result = operational_invariants.scan(self.connection, home=self.home, repo=self.home, now_epoch=10)
        self.assertFalse(result["healthy"])
        self.assertFalse(result["invariants"]["windows_control.native"]["healthy"])

    def test_persistent_failure_escalates_only_after_recovery_streak(self):
        failed = {
            "healthy": False, "reason": "still offline", "code_fixable": False,
        }
        with patch.object(operational_invariants, "_provider_invariant", return_value=failed), \
             patch.object(operational_invariants, "_native_control_state", return_value=self.healthy()), \
             patch.object(operational_invariants, "_delivery_invariant", return_value=self.healthy()), \
             patch.object(operational_invariants, "emit_attention") as attention:
            for index in range(3):
                result = operational_invariants.scan(
                    self.connection, home=self.home, repo=self.home, now_epoch=100 + index,
                )
        self.assertEqual(result["invariants"]["engineering.provider_continuity"]["failure_streak"], 3)
        attention.assert_called_once()

    def test_persistent_native_control_failure_submits_one_internal_repair(self):
        failed = {
            "healthy": False, "status": "offline", "age_seconds": 999,
            "reason": "native control offline", "code_fixable": True,
        }
        with patch.object(operational_invariants, "_provider_invariant", return_value=self.healthy()), \
             patch.object(operational_invariants, "_native_control_state", return_value=failed), \
             patch.object(operational_invariants, "_delivery_invariant", return_value=self.healthy()), \
             patch.object(operational_invariants, "_ready_engineering_providers", return_value=["ollama"]), \
             patch("life_os.engineering.submit", return_value={"id": 42, "created": True}) as submit, \
             patch.object(operational_invariants, "emit_attention"):
            for index in range(3):
                result = operational_invariants.scan(
                    self.connection, home=self.home, repo=self.home, now_epoch=1000 + index,
                )
        self.assertEqual(result["repairs_submitted"]["windows_control.native"]["run_id"], 42)
        submit.assert_called_once()
        saved = json.loads(get_state(self.connection, operational_invariants.STATE_KEY))
        self.assertEqual(saved["repairs"]["windows_control.native"]["run_id"], 42)

    def test_delivery_slo_distinguishes_merged_from_stalled_pr(self):
        self.connection.execute("PRAGMA foreign_keys=OFF")
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.connection.execute(
            """INSERT INTO engineering_promotions(
               run_id,approval_id,state,repo_slug,pr_number,head_sha,created_at,updated_at)
               VALUES(999,999,'ci_passed','owner/repo',7,'abc',?,?)""",
            (old, old),
        )
        self.connection.commit()
        with patch("life_os.engineering_delivery._gh", return_value=json.dumps({
            "state": "MERGED", "mergedAt": old, "headRefOid": "abc", "url": "https://example.invalid/pr/7",
        })):
            merged = operational_invariants._delivery_invariant(
                self.connection, self.home, now_epoch=datetime.now(timezone.utc).timestamp(),
            )
        self.assertTrue(merged["healthy"])
        with patch("life_os.engineering_delivery._gh", return_value=json.dumps({
            "state": "OPEN", "mergedAt": None, "headRefOid": "abc", "url": "https://example.invalid/pr/7",
        })):
            stalled = operational_invariants._delivery_invariant(
                self.connection, self.home, now_epoch=datetime.now(timezone.utc).timestamp(),
            )
        self.assertFalse(stalled["healthy"])
        self.assertEqual(stalled["stalled"][0]["pr_number"], 7)

    def test_disposable_database_does_not_gain_host_process_authority(self):
        result = autonomy_maintenance._operational_health(self.connection)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["skipped"], "runtime_not_installed")


if __name__ == "__main__":
    unittest.main()
