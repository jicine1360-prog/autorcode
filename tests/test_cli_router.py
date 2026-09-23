import os
import unittest

from harness import cli


class TestBackend(unittest.TestCase):
    def tearDown(self):
        for k in ("AGENT_BASE_URL", "AGENT_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)

    def test_local_default(self):
        base, key = cli._resolve_backend("phi4")
        self.assertEqual(key, "ollama")
        self.assertTrue(base.endswith("/v1"))

    def test_agent_base_url_wins(self):
        os.environ["AGENT_BASE_URL"] = "https://custom.example/v1"
        os.environ["AGENT_API_KEY"] = "secret"
        base, key = cli._resolve_backend("whatever/foo")
        self.assertEqual(base, "https://custom.example/v1")
        self.assertEqual(key, "secret")

    def test_openrouter_slash_model(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        base, key = cli._resolve_backend("deepseek/deepseek-chat-v3")
        self.assertEqual(base, "https://openrouter.ai/api/v1")
        self.assertEqual(key, "sk-or-test")

    def test_openrouter_requires_key(self):
        with self.assertRaises(SystemExit):
            cli._resolve_backend("deepseek/deepseek-chat-v3")
        self.assertNotIn("OPENROUTER_API_KEY", os.environ)