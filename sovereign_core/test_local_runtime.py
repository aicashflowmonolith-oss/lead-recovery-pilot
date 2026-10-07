import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_runtime


class LocalRuntimeTests(unittest.TestCase):
    def test_control_token_is_created_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            token_file = state / "control.token"
            with mock.patch.object(local_runtime, "STATE_DIR", state), mock.patch.object(
                local_runtime, "TOKEN_FILE", token_file
            ):
                first = local_runtime.ensure_control_token()
                second = local_runtime.ensure_control_token()
                self.assertEqual(first, second)
                self.assertGreaterEqual(len(first), 32)
                self.assertEqual(token_file.read_text(encoding="utf-8").strip(), first)


if __name__ == "__main__":
    unittest.main()
