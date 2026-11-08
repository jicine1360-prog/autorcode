"""체크포인트/롤백 + AGENTS.md 로딩 테스트."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import agent_core, config, tools


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _write(self, path, content):
        return tools.execute("write_file", {"path": path, "content": content},
                             self.root, 4000, 10)

    def _edit(self, path, old, new):
        return tools.execute("edit_file", {"path": path, "old_string": old,
                                           "new_string": new}, self.root, 4000, 10)

    def _rollback(self, cid=None):
        args = {"checkpoint": cid} if cid else {}
        return tools.execute("rollback", args, self.root, 4000, 10)

    def _read(self, path):
        with open(os.path.join(self.root, path), encoding="utf-8") as f:
            return f.read()

    def test_write_rollback_restores_original(self):
        self._write("a.txt", "원본")
        self.assertIn("체크포인트", self._write("a.txt", "틀린 내용"))
        self.assertIn("롤백 완료", self._rollback())
        self.assertEqual(self._read("a.txt"), "원본")

    def test_edit_rollback(self):
        self._write("b.txt", "hello world")
        self._edit("b.txt", "world", "earth")
        self.assertEqual(self._read("b.txt"), "hello earth")
        self._rollback()
        self.assertEqual(self._read("b.txt"), "hello world")

    def test_rollback_new_file_deletes_it(self):
        self._write("new.txt", "새 파일")  # 없을 때 쓰기 → existed=False
        self.assertTrue(os.path.isfile(os.path.join(self.root, "new.txt")))
        self._rollback()
        self.assertFalse(os.path.isfile(os.path.join(self.root, "new.txt")))

    def test_rollback_by_id(self):
        self._write("c.txt", "v1")
        r = self._write("c.txt", "v2")
        cid = r.split("체크포인트: ")[1].rstrip("]")
        self._write("c.txt", "v3")
        self.assertIn("롤백 완료", self._rollback(cid))
        self.assertEqual(self._read("c.txt"), "v1")

    def test_rollback_last_after_multiple(self):
        self._write("d.txt", "v1")
        self._write("d.txt", "v2")
        self._write("d.txt", "v3")
        self._rollback()  # last = v3 쓰기 직전 = v2
        self.assertEqual(self._read("d.txt"), "v2")

    def test_rollback_empty(self):
        self.assertIn("체크포인트 없음", self._rollback())

    def test_prune_keeps_recent(self):
        from harness import checkpoints
        checkpoints.KEEP = 3
        self.addCleanup(setattr, checkpoints, "KEEP", 50)
        self._write("e.txt", "v0")
        for i in range(1, 6):
            self._write("e.txt", f"v{i}")
        cps = checkpoints.list_checkpoints(self.root)
        self.assertEqual(len(cps), 3)
        # 가장 최근 체크포인트 = 마지막 쓰기(v5) 직전 상태 = v4
        self._rollback()
        self.assertEqual(self._read("e.txt"), "v4")


class TestAgentsMd(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_load(self):
        with open(os.path.join(self.root, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("이 폴더에서는 rm 금지.")
        got = agent_core._load_agents_md(self.root, 4000)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "AGENTS.md")
        self.assertIn("rm 금지", got[1])

    def test_dot_variant(self):
        with open(os.path.join(self.root, ".AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("규칙 테스트 마커")
        got = agent_core._load_agents_md(self.root, 4000)
        self.assertIn("규칙 테스트 마커", got[1])

    def test_missing_returns_none(self):
        self.assertIsNone(agent_core._load_agents_md(self.root, 4000))

    def test_empty_returns_none(self):
        with open(os.path.join(self.root, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("   \n")
        self.assertIsNone(agent_core._load_agents_md(self.root, 4000))

    def test_size_cap(self):
        with open(os.path.join(self.root, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("가" * 100)
        got = agent_core._load_agents_md(self.root, 50)
        self.assertEqual(len(got[1]), 50)

    def test_agent_injects_into_system(self):
        with open(os.path.join(self.root, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("시스템프롬프트 마커 규칙")
        cfg = config.load()
        cfg.workspace_root = self.root
        cfg.memory_file = os.path.join(self.root, "memory.json")
        agent = agent_core.Agent(cfg)  # mock 모드
        sysmsg = agent.mem.messages()[0]["content"]
        self.assertIn("시스템프롬프트 마커 규칙", sysmsg)


if __name__ == "__main__":
    unittest.main()
