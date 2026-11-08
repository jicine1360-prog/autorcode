"""AGENTS.md/CLAUDE.md 자동 로드 테스트 (네트워크 불필요)."""
import os
import tempfile
import unittest

from harness import instructions


class InstructionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, "home")
        self.ws = os.path.join(self.tmp, "proj", "sub")
        os.makedirs(os.path.join(self.home, ".autorcode"))
        os.makedirs(self.ws)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self._old_lim = os.environ.get("AGENT_INSTRUCTIONS_MAX_CHARS")
        os.environ.pop("AGENT_INSTRUCTIONS_MAX_CHARS", None)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        if self._old_lim is not None:
            os.environ["AGENT_INSTRUCTIONS_MAX_CHARS"] = self._old_lim

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_agents_md_loaded(self):
        self._write(os.path.join(self.ws, "AGENTS.md"), "테스트 지침: 한국어로 답하기")
        text, files = instructions.load(self.ws)
        self.assertIn("테스트 지침: 한국어로 답하기", text)
        self.assertEqual(files[-1], os.path.join(self.ws, "AGENTS.md"))

    def test_claude_md_and_nested_dir_variants(self):
        self._write(os.path.join(self.ws, "CLAUDE.md"), "클로드 지침")
        self._write(os.path.join(self.ws, ".autorcode", "instructions.md"), "autorcode 지침")
        text, files = instructions.load(self.ws)
        self.assertIn("클로드 지침", text)
        self.assertIn("autorcode 지침", text)
        self.assertEqual(len(files), 2)

    def test_order_global_then_ancestor_then_workspace(self):
        self._write(os.path.join(self.home, ".autorcode", "AGENTS.md"), "전역 지침")
        self._write(os.path.join(self.tmp, "proj", "AGENTS.md"), "조상 지침")
        self._write(os.path.join(self.ws, "AGENTS.md"), "작업공간 지침")
        text, _ = instructions.load(self.ws)
        positions = [text.index(m) for m in ("전역 지침", "조상 지침", "작업공간 지침")]
        self.assertEqual(positions, sorted(positions))

    def test_total_char_limit(self):
        os.environ["AGENT_INSTRUCTIONS_MAX_CHARS"] = "120"
        self._write(os.path.join(self.ws, "AGENTS.md"), "가" * 500)
        text, _ = instructions.load(self.ws)
        self.assertLessEqual(len(text), 120)

    def test_no_files_returns_empty(self):
        text, files = instructions.load(self.ws)
        self.assertEqual(text, "")
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
