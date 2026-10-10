"""목표(goal) — 저장/단계/삭제/정기 점검(scan_due) 테스트."""
import os
import shutil
import tempfile
import time
import unittest

from harness import goals, notes, tools


class GoalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AUTORCODE_GOALS_DIR"] = self.tmp
        self.chat = 50735853

    def tearDown(self):
        os.environ.pop("AUTORCODE_GOALS_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_list(self):
        out = goals.add(self.chat, "매일 산책", ["걷기", "기록"])
        self.assertIn("목표 등록", out)
        listing = goals.list_goals(self.chat)
        self.assertIn("매일 산책", listing)
        self.assertIn("0/2", listing)

    def test_step_completes_goal(self):
        goals.add(self.chat, "금주 리팩터", ["A"])
        key = goals._load(self.chat)[0]["key"]
        self.assertIn("완료", goals.step_done(self.chat, key))
        self.assertIn("✅", goals.list_goals(self.chat))

    def test_step_by_number_and_text(self):
        goals.add(self.chat, "운동", ["스쿼트", "플랭크"])
        key = goals._load(self.chat)[0]["key"]
        goals.step_done(self.chat, key, "2")
        goals.step_done(self.chat, key, "스쿼트")
        self.assertIn("✅", goals.list_goals(self.chat))

    def test_scan_due_bumps_next_check(self):
        goals.add(self.chat, "독서", ["1권"])
        g = goals._load(self.chat)[0]
        g["next_check"] = time.time() - 10
        goals._save(self.chat, [g])
        due = goals.scan_due(self.tmp)
        self.assertTrue(any(d[1].startswith("목표 점검") for d in due))
        g2 = goals._load(self.chat)[0]
        self.assertGreater(g2["next_check"], time.time())

    def test_scan_due_skips_future(self):
        goals.add(self.chat, "나중 목표", ["x"])
        self.assertEqual(goals.scan_due(self.tmp), [])

    def test_remove_and_empty(self):
        goals.add(self.chat, "삭제 목표")
        key = goals._load(self.chat)[0]["key"]
        self.assertIn("삭제", goals.remove(self.chat, key))
        self.assertIn("목표 없음", goals.list_goals(self.chat))

    def test_tool_wrappers(self):
        with notes.scope(str(self.chat)):
            tools.execute("goal_add", {"goal": "물 마시기", "steps": ["아침", "저녁"]}, None, 4000, 10)
            listing = tools.execute("goal_list", {}, None, 4000, 10)
        self.assertIn("물 마시기", listing)


if __name__ == "__main__":
    unittest.main()