import os
import tempfile
import unittest
from unittest.mock import patch

from harness import cli, config


class TestBackend(unittest.TestCase):
    def setUp(self):
        # ~/.autorcode/env 이 AGENT_BASE_URL 을 심어 뒤에도 각 테스트는 깨끗하게 시작.
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("AGENT_BASE_URL", "AGENT_API_KEY", "OPENROUTER_API_KEY")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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

    def test_installed_ollama_model_beats_custom_server(self):
        os.environ["AGENT_BASE_URL"] = "http://127.0.0.1:8000/v1"
        with patch("harness.cli._names", return_value=["phi4:latest"]):
            base, key = cli._resolve_backend("phi4")
        self.assertEqual(base, cli._host() + "/v1")
        self.assertEqual(key, "ollama")

    def test_uninstalled_model_goes_to_custom_server(self):
        os.environ["AGENT_BASE_URL"] = "http://127.0.0.1:8000/v1"
        with patch("harness.cli._names", return_value=["phi4:latest"]):
            base, key = cli._resolve_backend("qwen3.6-35b-nvfp4")
        self.assertEqual(base, "http://127.0.0.1:8000/v1")
        self.assertEqual(key, "ollama")

    def test_slash_model_skips_ollama_lookup(self):
        os.environ["AGENT_BASE_URL"] = "http://127.0.0.1:8000/v1"
        with patch("harness.cli._names", side_effect=AssertionError("ollama 조회 금지")):
            base, _ = cli._resolve_backend("deepseek/deepseek-chat-v3")
        self.assertEqual(base, "http://127.0.0.1:8000/v1")

    def test_openrouter_slash_model(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        base, key = cli._resolve_backend("deepseek/deepseek-chat-v3")
        self.assertEqual(base, "https://openrouter.ai/api/v1")
        self.assertEqual(key, "sk-or-test")

    def test_openrouter_requires_key(self):
        with self.assertRaises(SystemExit):
            cli._resolve_backend("deepseek/deepseek-chat-v3")
        self.assertNotIn("OPENROUTER_API_KEY", os.environ)


class TestModelCatalog(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("AGENT_BASE_URL", "AGENT_API_KEY", "OPENROUTER_API_KEY")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_ollama_only_without_custom_server(self):
        with patch("harness.cli._names", return_value=["phi4:latest"]):
            cat = cli.model_catalog()
        self.assertEqual(cat, [{"name": "phi4:latest", "base_url": cli._host() + "/v1",
                                "api_key": "ollama", "source": "ollama"}])

    def test_custom_server_appended_and_deduped(self):
        os.environ["AGENT_BASE_URL"] = "http://127.0.0.1:8000/v1"
        os.environ["AGENT_API_KEY"] = "dummy"
        with patch("harness.cli._names", return_value=["phi4:latest", "shared"]), \
             patch("harness.cli._openai_models",
                   return_value=["shared", "qwen3.6-35b-nvfp4"]):
            cat = cli.model_catalog()
        # ollama 가 먼저, 같은 이름은 ollama 항목이 살아남는다
        self.assertEqual([e["name"] for e in cat],
                         ["phi4:latest", "shared", "qwen3.6-35b-nvfp4"])
        self.assertEqual(cat[0]["source"], "ollama")
        self.assertEqual(cat[2], {"name": "qwen3.6-35b-nvfp4",
                                  "base_url": "http://127.0.0.1:8000/v1",
                                  "api_key": "dummy", "source": "127.0.0.1:8000"})

    def test_unreachable_custom_server_skipped(self):
        os.environ["AGENT_BASE_URL"] = "http://127.0.0.1:9/v1"
        with patch("harness.cli._names", return_value=["phi4:latest"]), \
             patch("harness.cli._openai_models", return_value=None):
            cat = cli.model_catalog()
        self.assertEqual([e["name"] for e in cat], ["phi4:latest"])


class TestEnvFile(unittest.TestCase):
    PROBE = "AGENT_ENVFILE_PROBE"

    def tearDown(self):
        for k in (self.PROBE, "AUTORCODE_ENV_FILE"):
            os.environ.pop(k, None)

    def test_values_load_and_existing_env_wins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(f"# 주석 줄\n\n{self.PROBE}=from-file\n"
                     f"{self.PROBE}_QUOTED='quo ted'\n")
            path = fh.name
        try:
            os.environ.pop(self.PROBE, None)
            os.environ.pop(self.PROBE + "_QUOTED", None)
            os.environ[self.PROBE] = "from-shell"
            config._load_env_file(path)
            self.assertEqual(os.environ[self.PROBE], "from-shell")   # sh 값이 우선
            self.assertEqual(os.environ[self.PROBE + "_QUOTED"], "quo ted")
        finally:
            os.unlink(path)
            os.environ.pop(self.PROBE + "_QUOTED", None)

    def test_path_override_env_var(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(f"{self.PROBE}=via-override\n")
            path = fh.name
        try:
            os.environ.pop(self.PROBE, None)
            os.environ["AUTORCODE_ENV_FILE"] = path
            config._load_env_file()
            self.assertEqual(os.environ[self.PROBE], "via-override")
        finally:
            os.unlink(path)

    def test_missing_file_is_silent(self):
        os.environ.pop(self.PROBE, None)
        config._load_env_file("/nonexistent/env-file-test")
        self.assertNotIn(self.PROBE, os.environ)

    def test_real_env_file_parses(self):
        """실제 ~/.autorcode/env 가 있으면 그 내용이 그대로 새어나오지 않는지."""
        path = os.path.expanduser("~/.autorcode/env")
        if not os.path.isfile(path):
            self.skipTest("~/.autorcode/env 없음")
        keys = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.append(line.partition("=")[0].strip())
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            config._load_env_file(path)
            for k in keys:
                self.assertIn(k, os.environ)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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
