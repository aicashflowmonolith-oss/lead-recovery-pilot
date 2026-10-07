import unittest

import render_runtime as runtime


class RuntimeAuthTests(unittest.TestCase):
    def test_valid_control_token_requires_minimum_length(self):
        self.assertFalse(runtime.valid_control_token(""))
        self.assertFalse(runtime.valid_control_token("x" * 31))
        self.assertTrue(runtime.valid_control_token("x" * 32))

    def test_authorized_header_accepts_only_exact_bearer_token(self):
        token = "a" * 48
        self.assertTrue(runtime.authorized_header("Bearer " + token, token))
        self.assertFalse(runtime.authorized_header(token, token))
        self.assertFalse(runtime.authorized_header("Bearer " + ("b" * 48), token))
        self.assertFalse(runtime.authorized_header(None, token))


if __name__ == "__main__":
    unittest.main()
