# -*- coding: utf-8 -*-
"""연속 실패 가드(시도 서명과 무관) 검증.

기존 LoopGuard는 '같은 시도'의 연속 실패만 잡는다. 실패마다 접근(서명)을
살짝 바꾸면 카운트가 초기화돼 "수정 없이 계속 진행"을 통과시킨다.
Agent._fail_streak 는 전체 연속 실패를 세어 steer(4)→abort(6) 로 막는다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import agent_core, config


class TestFailStreak(unittest.TestCase):
    def _agent(self):
        cfg = config.load()
        cfg.base_url = ""   # 빈 base_url → use_mock (LLM 네트워크 호출 없음)
        cfg.show_steps = False
        cfg.loop_limit = 3
        return agent_core.Agent(cfg)

    def _failed_triple(self, i):
        # 서명이 매번 달라야 기존 가드가 abort하지 않는다.
        # normalize()는 숫자를 지우므로 접미사는 반드시 문자로 구분한다 (a,b,c,...)
        suffix = chr(ord("a") + i)
        cmd = f"false_{suffix}"
        return ("bash", {"command": cmd}, f"[exit=1]\nboom {suffix}")

    def test_steer_at_4_abort_at_6(self):
        a = self._agent()
        steers, abort = [], ""
        for i in range(6):
            m, ab = a._loop_feedback([self._failed_triple(i)])
            if m:
                steers.append(m)
            if ab:
                abort = ab
                break
        self.assertTrue(
            any("연속으로 실패" in m for m in steers),
            "4연속 실패에서 진단 강제 안내가 나와야 한다")
        self.assertIn("중단", abort, "6연속 실패에서 작업이 중단되어야 한다")

    def test_success_resets_streak(self):
        a = self._agent()
        for i in range(3):
            a._loop_feedback([self._failed_triple(i)])
        a._loop_feedback([("bash", {"command": "true"}, "[exit=0]\nok")])
        self.assertEqual(a._fail_streak, 0)

    def test_alternating_failures_reach_steer(self):
        """다른 서명의 실패가 끼어도 streak이 초기화되지 않는다 (핵심 구멍)."""
        a = self._agent()
        steers = 0
        for i in range(4):
            # 실패마다 도구도 바꿔 가드 서명을 완전히 분리한다
            tool = ["bash", "list_dir", "grep_files", "read_file"][i]
            args = ({"command": f"bad{i}"} if tool == "bash" else {"path": f"/x{i}"})
            m, ab = a._loop_feedback([(tool, args, "[exit=1]\nerr")])
            if m and "연속으로 실패" in m:
                steers += 1
            self.assertFalse(ab, "서명이 달라도 4연속이면 abort하면 안 되고 steer가 먼저다")
        self.assertEqual(steers, 1)


if __name__ == "__main__":
    unittest.main()
