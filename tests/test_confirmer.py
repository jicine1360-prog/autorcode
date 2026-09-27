"""승인 경로 선택 (_confirmer).

여기서 검증하는 것: 어떤 플래그에서 어떤 경로가 쓰이는지, 그리고 특히
--approve telegram 을 줬는데 게이트를 못 만들면 조용히 거부로 떨어지지 않는가.
승인을 기다릴 것처럼 보여놓고 아무 것도 오지 않는 상황이 최악이라, 그건 조용히
삼키면 안 된다.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import cli  # noqa: E402


class ConfirmerRoutingTest(unittest.TestCase):
    def test_tty_uses_tty_in_auto_mode(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch("harness.approve.build_from_config") as build:
            self.assertIs(cli._confirmer(mode="auto"), cli._ask)
        build.assert_not_called()  # 게이트를 띄우면 폴러 스레드가 도는 무의미한 비용

    def test_no_tty_uses_telegram_in_auto_mode(self):
        with patch.object(sys.stdin, "isatty", return_value=False), \
             patch("harness.approve.build_from_config", return_value="GATE"):
            self.assertEqual(cli._confirmer(mode="auto"), "GATE")

    def test_no_tty_without_config_denies(self):
        with patch.object(sys.stdin, "isatty", return_value=False), \
             patch("harness.approve.build_from_config", return_value=None):
            self.assertIsNone(cli._confirmer(mode="auto"))

    def test_tty_mode_ignores_telegram_even_on_tty(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch("harness.approve.build_from_config") as build:
            self.assertIs(cli._confirmer(mode="tty"), cli._ask)
        build.assert_not_called()

    def test_deny_mode_denies_without_touching_anything(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch("harness.approve.build_from_config") as build:
            self.assertIsNone(cli._confirmer(mode="deny"))
        build.assert_not_called()

    def test_telegram_mode_forces_gate_on_tty(self):
        # 터미널에서 실행해도 폰으로 승인받으려는 경로.
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch("harness.approve.build_from_config", return_value="GATE"):
            self.assertEqual(cli._confirmer(mode="telegram"), "GATE")

    def test_telegram_mode_without_config_fails_loudly(self):
        # 조용히 None 을 돌려주면 사용자는 승인을 기다리다 아무 것도 오지 않는 걸
        # 그대로 경험한다. 반드시 오류로 끝나야 한다.
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch("harness.approve.build_from_config", return_value=None), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli._confirmer(mode="telegram")
        self.assertIn("승인 게이트를 쓸 수 없습니다", str(cm.exception))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(SystemExit):
            cli._confirmer(mode="nonsense")


class ApproveFlagParserTest(unittest.TestCase):
    def test_help_lists_approve_flag(self):
        out = io.StringIO()
        with patch("sys.argv", ["autorcode", "run", "--help"]), redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertIn("--approve", out.getvalue())
        for choice in ("auto", "telegram", "tty", "deny"):
            self.assertIn(choice, out.getvalue())


if __name__ == "__main__":
    unittest.main()
