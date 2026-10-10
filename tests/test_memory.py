"""기억 예산·반성 요약 테스트 (네트워크 불필요)."""
import os
import tempfile
import unittest

from harness import notes, tools


class MemoryBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mem = os.path.join(self.tmp, "memory.txt")
        os.environ["AGENT_MEMORY_FILE"] = self.mem
        os.environ["AUTORCODE_MEMORY_MAX_LINES"] = "4"

    def tearDown(self):
        os.environ.pop("AGENT_MEMORY_FILE", None)
        os.environ.pop("AUTORCODE_MEMORY_MAX_LINES", None)

    def test_append_trims_oldest(self):
        for i in range(1, 7):
            notes.append(f"사실{i}")
        st = notes.stats()
        self.assertLessEqual(st["lines"], 4)
        self.assertIn("사실6", notes.load())
        self.assertNotIn("사실1", notes.load())

    def test_set_summary_keeps_latest_three(self):
        for i in range(1, 8):
            notes.append(f"줄{i}")
        notes.set_summary("핵심만 요약")
        text = notes.load()
        self.assertIn("* 요약: 핵심만 요약", text)
        self.assertEqual(text.count("- "), 3)
        self.assertTrue(notes.stats()["summary"])

    def test_reflect_uses_injected_summarizer(self):
        notes.append("A")
        notes.append("B")
        out = notes.reflect(lambda t: "A와 B 요약")
        self.assertIn("A와 B 요약", notes.load())
        self.assertIn("갱신", out)

    def test_empty_memory_reflect_skipped(self):
        self.assertIn("반성 생략", notes.reflect(lambda t: "x"))
        self.assertIn("오류", notes.set_summary(""))

    def test_tool_wrappers(self):
        with notes.scope("777"):
            tools.execute("remember", {"fact": "테스트"}, None, 4000, 10)
            out = tools.execute("memory_reflect", {}, None, 4000, 10)
            self.assertIn("memory_save_summary", out)
            tools.execute("memory_save_summary", {"summary": "정리됨"}, None, 4000, 10)
            self.assertIn("정리됨", notes.load())


if __name__ == "__main__":
    unittest.main()