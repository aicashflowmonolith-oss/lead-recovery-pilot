import tempfile
import unittest
from pathlib import Path

import autonomy
import monolith as core


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "core.db")
        core.init_db(self.db)
        self.conn = core.connect(self.db)
        autonomy.init_autonomy(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_goal_advances_without_operator_commands(self):
        goal = autonomy.create_goal(
            self.conn,
            "prove autonomous sequencing",
            "complete two verified tasks in order",
            [
                {"kind": "echo", "payload": {"step": 1}},
                {"kind": "state.set", "payload": {"key": "autonomy.proof", "value": True}},
            ],
            priority=10,
        )
        self.assertEqual(goal["status"], "active")

        first_tick = autonomy.tick(self.conn)
        self.assertEqual(first_tick["goals"]["submitted"], 1)
        first_task = core.work_once(self.conn)
        self.assertEqual(first_task["status"], "succeeded")

        second_tick = autonomy.tick(self.conn)
        self.assertEqual(second_tick["goals"]["submitted"], 1)
        second_task = core.work_once(self.conn)
        self.assertEqual(second_task["status"], "succeeded")

        final_tick = autonomy.tick(self.conn)
        self.assertEqual(final_tick["goals"]["completed"], 1)
        final = autonomy.get_goal(self.conn, goal["id"])
        self.assertEqual(final["status"], "completed")
        self.assertTrue(all(step["status"] == "succeeded" for step in final["steps"]))

    def test_due_schedule_submits_once_and_moves_forward(self):
        schedule = autonomy.add_schedule(
            self.conn,
            "heartbeat-proof",
            "echo",
            {"scheduled": True},
            interval_seconds=60,
            next_run=0,
        )
        self.assertTrue(schedule["enabled"])
        fired = autonomy.run_due_schedules(self.conn)
        self.assertEqual(fired, 1)
        self.assertEqual(autonomy.run_due_schedules(self.conn), 0)
        task = core.work_once(self.conn)
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["result"], {"scheduled": True})


if __name__ == "__main__":
    unittest.main()
