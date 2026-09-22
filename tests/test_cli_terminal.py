"""실제 CLI 프로세스와 PTY로 stderr 스피너/stdout 결과 분리를 검증한다."""
import errno
import json
import os
import pty
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CLI = Path(__file__).resolve().parents[1] / "harness" / "cli.py"


class TerminalTests(unittest.TestCase):
    def test_installed_style_cli_with_streaming_and_real_terminal(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"models": [{"name": "test:local"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                observed = any(m["content"].startswith("[도구 결과]") for m in payload["messages"])
                action = {"done": True, "answer": "terminal verified"} if observed else {
                    "tool": "list_dir", "args": {"path": "."}}
                text = json.dumps(action)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                # 서버 첫 바이트 대기 및 응답 수신 중에도 터미널 갱신이 살아있어야 한다.
                time.sleep(0.25)
                for chunk in (text[:10], text[10:]):
                    event = {"choices": [{"delta": {"content": chunk}}]}
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(0.2)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        master, slave = pty.openpty()
        captured = bytearray()
        try:
            with tempfile.TemporaryDirectory() as workspace:
                env = dict(os.environ, TERM="xterm", OLLAMA_HOST=f"http://127.0.0.1:{server.server_port}",
                           AGENT_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", AGENT_API_KEY="",
                           AGENT_WORKSPACE=workspace, AGENT_SESSION="", AGENT_API_TIMEOUT="5",
                           AGENT_API_RETRIES="1", AGENT_SHOW_STEPS="1", AGENT_STREAM="1")
                with subprocess.Popen([sys.executable, str(CLI), "run", "test:local",
                                       "list files", "--details"], cwd=workspace, env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=slave) as proc:
                    os.close(slave)
                    slave = None
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(master, selectors.EVENT_READ)
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline:
                                if not selector.select(0.1):
                                    if proc.poll() is not None:
                                        break
                                    continue
                                try:
                                    data = os.read(master, 65536)
                                except OSError as error:
                                    if error.errno == errno.EIO:
                                        break
                                    raise
                                if not data:
                                    break
                                captured.extend(data)
                        stdout, _ = proc.communicate(timeout=2)
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()
                    self.assertEqual(proc.returncode, 0, captured.decode("utf-8", "replace"))
                    self.assertIn(b"terminal verified", stdout)
                    self.assertNotIn("모델 응답 대기".encode(), stdout)
                    terminal = captured.decode("utf-8")
                    self.assertIn("\x1b[2K", terminal)
                    self.assertIn("응답 수신 중", terminal)
                    self.assertIn("1.1 list_dir", terminal)
                    self.assertIn("[완료]", terminal)
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
