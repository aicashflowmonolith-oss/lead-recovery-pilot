import tempfile
import unittest
from pathlib import Path

import monolith as m


class SovereignCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "core.db")
        m.init_db(self.db)
        self.conn = m.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_echo_executes_and_verifies(self):
        task = m.submit_task(self.conn, "echo", {"text": "hello"})
        self.assertEqual(task["status"], "pending")
        done = m.work_once(self.conn)
        self.assertIsNotNone(done)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"], {"text": "hello"})
        self.assertTrue(done["verification"]["ok"])

    def test_state_round_trip(self):
        m.submit_task(
            self.conn,
            "state.set",
            {"key": "objective", "value": {"name": "maximize verified outcomes"}, "source": "test"},
        )
        write = m.work_once(self.conn)
        self.assertEqual(write["status"], "succeeded")

        m.submit_task(self.conn, "state.get", {"key": "objective"})
        read = m.work_once(self.conn)
        self.assertTrue(read["result"]["found"])
        self.assertEqual(read["result"]["value"]["name"], "maximize verified outcomes")

    def test_medium_risk_requires_explicit_approval(self):
        m.BUILTINS["medium.echo"] = (
            "medium",
            "Approval-gated test adapter.",
            m.execute_echo,
            m.verify_echo,
        )
        try:
            m.init_db(self.db)
            task = m.submit_task(self.conn, "medium.echo", {"x": 1})
            self.assertEqual(task["status"], "waiting_approval")
            self.assertIsNone(m.work_once(self.conn))
            approved = m.approve_task(self.conn, task["id"])
            self.assertEqual(approved["status"], "pending")
            done = m.work_once(self.conn)
            self.assertEqual(done["status"], "succeeded")
        finally:
            m.BUILTINS.pop("medium.echo", None)

    def test_failure_retries_then_stops(self):
        def fail(_conn, _payload):
            raise RuntimeError("expected failure")

        def verify(_conn, _payload, _result):
            return False

        m.BUILTINS["test.fail"] = ("low", "Failing test adapter.", fail, verify)
        try:
            m.init_db(self.db)
            task = m.submit_task(self.conn, "test.fail", {}, max_attempts=2)
            first = m.work_once(self.conn)
            self.assertEqual(first["status"], "pending")
            self.conn.execute("UPDATE tasks SET not_before=0 WHERE id=?", (task["id"],))
            second = m.work_once(self.conn)
            self.assertEqual(second["status"], "failed")
            self.assertEqual(second["attempts"], 2)
        finally:
            m.BUILTINS.pop("test.fail", None)

    def test_command_surface(self):
        set_result = m.command(self.conn, 'set mode "autonomous"')
        self.assertEqual(set_result["status"], "succeeded")
        get_result = m.command(self.conn, "get mode")
        self.assertEqual(get_result["result"]["value"], "autonomous")
        status = m.command(self.conn, "status")
        self.assertTrue(status["ok"])

    def test_stale_running_task_is_recovered(self):
        task = m.submit_task(self.conn, "echo", {"n": 1})
        self.conn.execute(
            "UPDATE tasks SET status='running',updated_at=? WHERE id=?",
            (m.now() - 1000, task["id"]),
        )
        recovered = m.recover_stale(self.conn, stale_seconds=300)
        self.assertEqual(recovered, 1)
        row = self.conn.execute("SELECT status,last_error FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIn("recovered", row["last_error"])


if __name__ == "__main__":
    unittest.main()
