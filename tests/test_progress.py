"""실행 전/대기 중/완료 후의 이벤트 순서와 출력 스트림을 검증한다."""
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from harness.agent_core import Agent, parse_action
from harness.cli import main, repl_command
from harness.config import Config
from harness.llm import LLMError, LengthError
from harness.progress import Progress, result_status, terminal_line, tool_label


class ScriptedModel:
    def __init__(self, *actions):
        self.actions = iter(actions)

    def chat(self, messages, model, **options):
        action = next(self.actions)
        if isinstance(action, BaseException):
            raise action
        return json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else action


class ProgressTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = io.StringIO()
        self.progress = Progress(stream=self.output, interval=0.02)
        self.agent = Agent(Config(base_url="", workspace_root=tmp.name, session_file=""),
                           progress=self.progress)
        self.addCleanup(self.agent.close)

    def test_wait_is_visible_before_model_response(self):
        with self.progress.activity("모델 응답 대기") as activity:
            self.assertIn("모델 응답 대기", self.output.getvalue())
            self.progress.update(activity, "응답 수신 중 · 50자")
            deadline = time.monotonic() + 2
            while "[진행]" not in self.output.getvalue() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("응답 수신 중 · 50자", self.output.getvalue())
        self.assertNotIn("\x1b", self.output.getvalue())
        stopped = self.output.getvalue()
        time.sleep(0.06)
        self.assertEqual(stopped, self.output.getvalue())

    def test_quiet_and_error_status(self):
        self.progress.enabled = False
        with self.progress.activity("hidden"):
            self.progress.result("tool", "[exit=1]\nbad", 0.1)
        self.assertEqual(self.output.getvalue(), "")
        for response, expected in (
            ("[exit=1]\nfailed", "실패"), ("[exit=0]\nok", "완료"),
            ("[오류] [거부] 승인 없음", "거부"),
            ("[exit=-9]\n[TIMEOUT] timeout", "시간초과"),
        ):
            self.assertEqual(result_status(response), expected)

    def test_preview_sanitizes_controls_and_limits_output(self):
        self.progress.result("test", "\x1b[2Jfirst\n" + "a\n" * 20, 1)
        self.assertNotIn("\x1b", self.output.getvalue())
        self.assertIn("--details", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("│"), 3)
        self.assertNotIn("SECRET", tool_label("write_file", {"path": "x", "content": "SECRET"}))
        self.assertLessEqual(len(terminal_line("가" * 60, 30)), 16)

    def test_tty_clears_spinner_on_exception(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        tty = TTY()
        with patch.dict(os.environ, {"TERM": "xterm"}):
            display = Progress(stream=tty)
        with self.assertRaises(RuntimeError):
            with display.activity("대기"):
                deadline = time.monotonic() + 2
                while "\r" not in tty.getvalue() and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise RuntimeError("stop")
        self.assertIn("\r", tty.getvalue())
        self.assertTrue(tty.getvalue().endswith("\x1b[2K"))

    def test_loop_shows_actual_work_without_raw_thought(self):
        self.agent.llm = ScriptedModel(
            {"thought": "DO_NOT_DISPLAY", "tool": "list_dir", "args": {"path": "."}},
            {"done": True, "answer": "finished"})
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            answer = self.agent.run("파일 목록")
        self.assertEqual(stdout.getvalue(), "")
        output = self.output.getvalue()
        for label in ("모델 응답 대기", "수신 완료", "list_dir", "[완료]", "2스텝"):
            self.assertIn(label, output)
        self.assertNotIn("DO_NOT_DISPLAY", output)
        self.assertNotIn('"tool":', output)
        self.assertIn("finished", answer)

    def test_length_limit_retries_shorter_not_abort(self):
        self.agent.llm = ScriptedModel(
            LengthError("길이 제한"), LengthError("길이 제한"),
            {"done": True, "answer": "finally short"})
        answer = self.agent.run("test")
        self.assertIn("finally short", answer)
        output = self.output.getvalue()
        self.assertIn("답변 길이 초과", output)
        self.assertIn("2/3", output)
        self.assertNotIn("LLM 호출 불가", output)

    def test_length_limit_gives_up_after_three(self):
        self.agent.llm = ScriptedModel(
            LengthError("길이 제한"), LengthError("길이 제한"), LengthError("길이 제한"))
        answer = self.agent.run("test")
        self.assertIn("AGENT_MAX_TOKENS", answer)
        self.assertIn("3/3", self.output.getvalue())

    def test_malformed_reply_visible_and_recovered(self):
        self.agent.llm = ScriptedModel(
            {"actions": [None]}, {"done": True, "answer": "recovered"})
        self.assertIn("recovered", self.agent.run("test"))
        self.assertIn("형식 재요청 1/3", self.output.getvalue())

    def test_prose_reply_accepted_as_direct_answer(self):
        self.agent.llm = ScriptedModel("네, 텔레그램 봇 상태를 확인해볼게요.")
        answer = self.agent.run("봇 고쳐줘")
        self.assertIn("상태를 확인해볼게요", answer)
        self.assertIn("평문 답변을 바로 답으로 인정", self.output.getvalue())
        self.assertNotIn("형식 재요청", self.output.getvalue())

    def test_llm_failure_and_interrupt_stop_cleanly(self):
        self.agent.llm = ScriptedModel(LLMError("offline"))
        self.assertIn("offline", self.agent.run("test"))
        self.assertIn("[실패]", self.output.getvalue())
        self.agent.llm = ScriptedModel(KeyboardInterrupt())
        self.assertIn("현재 작업 취소", self.agent.run("test"))
        self.assertFalse(any(t.name == "autorcode-progress" for t in threading.enumerate()))

    def test_excess_batch_is_repaired_not_silently_dropped(self):
        self.agent.cfg.max_actions = 1
        self.agent.llm = ScriptedModel(
            {"actions": [{"tool": "list_dir"}, {"tool": "list_dir"}]},
            {"done": True, "answer": "recovered"})
        with patch("harness.tools.execute") as execute:
            self.agent.run("test")
        execute.assert_not_called()
        self.assertIn("한 단계 도구 한도", self.output.getvalue())

    def test_parallel_result_visible_in_completion_order(self):
        release_slow, fast_visible = threading.Event(), threading.Event()
        class Output(io.StringIO):
            def write(self, text):
                result = super().write(text)
                if "[완료] 1.2" in text:
                    fast_visible.set()
                return result
        self.progress.stream = Output()
        def execute(name, args, *unused):
            if args["url"] == "slow":
                release_slow.wait(3)
            return args["url"]
        responses = []
        batch = {"actions": [
            {"tool": "web_fetch", "args": {"url": "slow"}},
            {"tool": "web_fetch", "args": {"url": "fast"}}]}
        with patch("harness.tools.execute", side_effect=execute):
            worker = threading.Thread(target=lambda: responses.append(self.agent._run_action(batch)))
            worker.start()
            try:
                self.assertTrue(fast_visible.wait(2), "빠른 결과가 느린 요청 뒤에 가려짐")
                self.assertNotIn("[완료] 1.1", self.progress.stream.getvalue())
            finally:
                release_slow.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        # 화면은 완료 순서, 모델에게 보내는 관측은 요청 순서를 유지한다.
        self.assertLess(responses[0].index("slow"), responses[0].index("fast"))

    def test_write_then_read_keeps_dependency_order(self):
        result = self.agent._run_action({"actions": [
            {"tool": "write_file", "args": {"path": "new.txt", "content": "marker"}},
            {"tool": "read_file", "args": {"path": "new.txt"}}]})
        self.assertIn("1: marker", result)
        self.assertIn("순서대로 실행", self.output.getvalue())

    def test_approvals_are_serial_on_main_thread(self):
        owners = []
        self.agent.confirmer = lambda why: owners.append(threading.get_ident()) or False
        with patch("harness.tools.execute") as execute:
            result = self.agent._run_action({"actions": [
                {"tool": "bash", "args": {"command": "python3 a.py"}},
                {"tool": "bash", "args": {"command": "python3 b.py"}}]})
        execute.assert_not_called()
        self.assertEqual(owners, [threading.get_ident()] * 2)
        self.assertIn("[거부]", result)
        self.assertIn("승인 대기", self.output.getvalue())

    def test_repl_toggles_do_not_call_model(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for command in ("/steps off", "/details on", "/tools", "/status", "/help"):
                self.assertTrue(repl_command(command, self.agent))
        self.assertFalse(self.progress.enabled)
        self.assertTrue(self.progress.details)
        self.assertFalse(repl_command("summarize", self.agent))

    def test_invalid_actions_rejected_before_execution(self):
        for action in ({"done": "false"}, {"actions": []}, {"tool": "bash", "args": []},
                       {"done": True, "answer": "x", "tool": "bash"}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                parse_action(json.dumps(action))

    def test_cli_quiet_keeps_stdout_clean(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["autorcode", "run", "test:latest", "do it", "--quiet"]), \
             patch("harness.cli._names", return_value=["test:latest"]), \
             patch("harness.cli.config.setup_logging"), \
             patch("harness.agent_core.llm.OpenAICompatibleLLM", return_value=ScriptedModel(
                 {"done": True, "answer": "final answer"})), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(), 0)
        self.assertIn("final answer", stdout.getvalue())
        self.assertNotIn("모델 응답 대기", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_options_between_model_and_prompt(self):
        with patch("sys.argv", ["autorcode", "run", "test:latest", "--details", "--no-stream", "do it"]), \
             patch("harness.cli._names", return_value=["test:latest"]), \
             patch("harness.cli.config.setup_logging"), \
             patch("harness.agent_core.llm.OpenAICompatibleLLM", return_value=ScriptedModel(
                 {"done": True, "answer": "ok"})), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
