"""입력 종료(EOF)와 LLM 백엔드 부재 시 종료 동작을 검증한다.

- autorcode run REPL에서 Ctrl+D(EOF)는 즉시 종료해야 한다. 이전에는 _input()이
  EOF에서 빈 문자열을 반환해 `if not task: continue`가 무한 루프를 돌았다.
- ollama가 없고 OPENROUTER_API_KEY도 없으면 즉시 안내와 함께 종료해야 한다.
  이전에는 죽은 127.0.0.1:11434로 조용히 진행해 3회 재시도 후 의미 없는
  "Connection refused"만 남았다.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "harness" / "cli.py"
DEAD_PORT = 59999  # 이 포트에 리스닝하는 것은 없다


def _base_env(**extra):
    """ollama가 확실히 죽어 있고 OpenRouter 키도 없는 환경."""
    env = dict(os.environ)
    env.pop("OPENROUTER_API_KEY", None)
    env.update(OLLAMA_HOST=f"http://127.0.0.1:{DEAD_PORT}", AGENT_BASE_URL="",
               AGENT_API_KEY="", OPENROUTER_MODEL="", AGENT_API_RETRIES="1",
               AGENT_API_TIMEOUT="2", AGENT_SHOW_STEPS="0", AGENT_STREAM="1")
    env.update(extra)
    return env


class ReplEofTests(unittest.TestCase):
    def test_repl_exits_immediately_on_eof(self):
        """stdin이 닫힌 상태로 REPL을 띄우면 프롬프트 루프를 돌지 않고 끝나야 한다."""
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # /api/tags — REPL 진입에 필요한 모델 목록
                body = json.dumps({"models": [{"name": "test:local", "size": 1}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as workspace:
                env = _base_env(
                    AGENT_WORKSPACE=workspace, AGENT_SESSION="",
                    OLLAMA_HOST=f"http://127.0.0.1:{server.server_port}",
                    AGENT_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1")
                with subprocess.Popen(
                        [sys.executable, str(CLI), "run", "test:local"],
                        cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                    try:
                        stdout, stderr = proc.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                        self.fail("EOF에서 autorcode run REPL이 종료되지 않음 (무한 루프)")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

        out = stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, stderr.decode("utf-8", "replace"))
        # '당신> ' 프롬프트가 무한 반복되지 않았는지 확인한다.
        self.assertLessEqual(out.count("당신> "), 1,
                             f"EOF 이후 프롬프트가 반복됨: {out.count('당신> ')}회")

    def test_run_without_ollama_and_without_key_fails_fast(self):
        with tempfile.TemporaryDirectory() as workspace:
            env = _base_env(AGENT_WORKSPACE=workspace, AGENT_SESSION="")
            result = subprocess.run(
                [sys.executable, str(CLI), "run", "phi4", "테스트", "--no-session"],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=30)
        out = result.stdout.decode("utf-8", "replace")
        err = result.stderr.decode("utf-8", "replace")
        combined = out + err
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OPENROUTER_API_KEY", combined)
        self.assertIn("ollama", combined)
        # 재시도 3회를 돌며 "API 재시도"를 찍으면 안 된다.
        self.assertNotIn("API 재시도", combined)


class MissingBackendTests(unittest.TestCase):
    def test_list_reports_openrouter_alternative(self):
        """autorcode list도 ollama만 안내하지 말고 키 경로를 함께 알려줘야 한다."""
        with tempfile.TemporaryDirectory() as workspace:
            env = _base_env(AGENT_WORKSPACE=workspace)
            result = subprocess.run([sys.executable, str(CLI), "list"],
                                    cwd=workspace, env=env,
                                    stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=30)
        out = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OPENROUTER_API_KEY", out)

    def test_run_with_key_falls_back_to_openrouter(self):
        """키가 있으면 죽은 ollama에서도 OpenRouter로 진행해야 한다."""
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                seen["auth"] = self.headers.get("Authorization", "")
                seen["payload"] = json.loads(self.rfile.read(length))
                body = json.dumps({"choices": [{"message": {"content":
                    '{"done": true, "answer": "cloud ok"}'}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # OpenRouter 도메인은 config가 http(s) 요구를 하지만 실제 요청은 가로챈다.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as workspace:
                env = _base_env(
                    AGENT_WORKSPACE=workspace, AGENT_SESSION="",
                    AGENT_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
                    AGENT_API_KEY="sk-or-test", OPENROUTER_MODEL="deepseek/test-model")
                result = subprocess.run(
                    [sys.executable, str(CLI), "run", "deepseek/test-model",
                     "테스트", "--no-session"],
                    cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=30)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
        out = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("cloud ok", out)
        self.assertEqual(seen.get("auth"), "Bearer sk-or-test")
        self.assertEqual(seen.get("payload", {}).get("model"), "deepseek/test-model")


if __name__ == "__main__":
    unittest.main()
