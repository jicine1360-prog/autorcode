"""가드 카운터 영속화 — 같은 작업은 세션 재개 후에도 이어가고 새 작업은 초기화."""
import io
import json
import os
import tempfile
import time
import unittest

from harness import agent_core
from harness.agent_core import Agent
from harness.config import Config
from harness.progress import Progress

BROKEN = '{"tool": "broken"'          # 키 힌트는 있으나 파싱 실패 → 위반 카운트


class ScriptedModel:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls = 0

    def chat(self, messages, model, **options):
        self.calls += 1
        action = self.actions.pop(0)
        text = json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else action
        return type("R", (), {"content": text, "tool_calls": None})()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sess = os.path.join(self.tmp, "s.jsonl")

    def _agent(self):
        agent = Agent(Config(base_url="", workspace_root=self.tmp, session_file=self.sess),
                      progress=Progress(stream=io.StringIO(), interval=0.02))
        self.addCleanup(agent.close)
        return agent

    def test_same_task_resumes_counters_after_abort(self):
        task = "반복 루프 작업"
        a1 = self._agent()
        a1.llm = ScriptedModel(*([BROKEN] * 3))
        out1 = a1.run(task)
        self.assertIn("3회 위반", out1)
        self.assertTrue(os.path.exists(self.sess + ".guard.json"))

        a2 = self._agent()
        model2 = ScriptedModel(BROKEN, BROKEN, BROKEN)
        a2.llm = model2
        out2 = a2.run(task)                       # 카운터 3 → 첫 위반에서 즉시 중단
        self.assertIn("3회 위반", out2)
        self.assertEqual(model2.calls, 1)

    def test_different_task_starts_fresh(self):
        a1 = self._agent()
        a1.llm = ScriptedModel(*([BROKEN] * 3))
        a1.run("첫 번째 작업")
        a2 = self._agent()
        model2 = ScriptedModel(BROKEN, {"done": True, "answer": "ok"})
        a2.llm = model2
        out = a2.run("완전히 다른 두 번째 작업")
        self.assertIn("ok", out)                  # 위반 이어받지 않음 → 정상 완료
        self.assertNotIn("3회 위반", out)

    def test_successful_run_clears_guard(self):
        task = "같은 작업"
        a1 = self._agent()
        a1.llm = ScriptedModel(*([BROKEN] * 3))
        a1.run(task)
        guard = self.sess + ".guard.json"
        self.assertTrue(os.path.exists(guard))
        a2 = self._agent()
        a2.llm = ScriptedModel({"done": True, "answer": "드디어 완료"})
        out = a2.run(task)
        self.assertIn("드디어 완료", out)
        self.assertFalse(os.path.exists(guard))   # 성공 시 가드 파일 삭제

    def test_ttl_expiry_resets_counters(self):
        task = "시간 지난 작업"
        agent = self._agent()
        agent_core._save_guard(agent.cfg, task, 2, 1, {"k": 1})
        guard = self.sess + ".guard.json"
        with open(guard, encoding="utf-8") as fh:
            data = json.load(fh)
        data["updated"] = time.time() - 99999
        with open(guard, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertEqual(agent_core._load_guard(agent.cfg, task), [0, 0, {}])

    def test_no_session_file_never_persists(self):
        agent = Agent(Config(base_url="", workspace_root=self.tmp, session_file=""),
                      progress=Progress(stream=io.StringIO(), interval=0.02))
        self.addCleanup(agent.close)
        agent_core._save_guard(agent.cfg, "t", 1, 0, {})
        self.assertEqual(agent_core._guard_path(agent.cfg), "")
        self.assertEqual(agent_core._load_guard(agent.cfg, "t"), [0, 0, {}])


if __name__ == "__main__":
    unittest.main()
