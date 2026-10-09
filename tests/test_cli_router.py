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


class TestMakeCfgReasoning(unittest.TestCase):
    """로컬 thinking 모델은 추론을 기본으로 끄고, 플래그가 우선한다."""

    def setUp(self):
        for k in ("AGENT_BASE_URL", "AGENT_API_KEY", "AGENT_REASONING_EFFORT"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("AGENT_BASE_URL", "AGENT_API_KEY", "AGENT_REASONING_EFFORT"):
            os.environ.pop(k, None)

    def _rest(self, **kw):
        import argparse
        base = dict(yes=False, session=None, no_session=True, auto=False,
                    quiet=True, details=False, no_stream=False,
                    max_tokens=None, reasoning_effort=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_local_defaults_to_none(self):
        cfg = cli._make_cfg("phi4", self._rest())
        self.assertEqual(cfg.reasoning_effort, "none")

    def test_flag_overrides_local_default(self):
        cfg = cli._make_cfg("phi4", self._rest(reasoning_effort="high"))
        self.assertEqual(cfg.reasoning_effort, "high")

    def test_cloud_keeps_empty(self):
        os.environ["AGENT_BASE_URL"] = "https://api.openai.com/v1"
        os.environ["AGENT_API_KEY"] = "sk-test"
        cfg = cli._make_cfg("gpt-4o", self._rest())
        self.assertEqual(cfg.reasoning_effort, "")
