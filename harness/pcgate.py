#!/usr/bin/env python3
"""pcgate — Windows PC 폴링 에이전트용 명령 게이트 (포트 8791)
PC가 /poll로 명령을 가져가고, /result로 결과를 올린다.
아우토(Open WebUI 도구)는 /submit으로 명령을 넣고 /result/<id>로 기다린다.
"""
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = ""
if os.path.exists(os.path.expanduser("~/.autorcode/pcgate.token")):
    TOKEN = open(os.path.expanduser("~/.autorcode/pcgate.token")).read().strip()

ALLOWED = {"sysinfo", "disk", "procs", "updates", "speak", "lock", "sleep", "restart", "shutdown"}
DANGEROUS = {"restart", "shutdown", "sleep"}

QUEUES = {}   # pc -> [{"cmd_id","cmd","text"}]
RESULTS = {}  # cmd_id -> {"status","output","ts"}
LAST_POLL = {}  # pc -> ts
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        path = self.path.rstrip("/")
        body = self._body()
        if body.get("token") != TOKEN:
            return self._json(401, {"ok": False, "error": "토큰 불일치"})
        if path == "/poll":
            pc = str(body.get("pc") or "pc")
            with LOCK:
                LAST_POLL[pc] = time.time()
                q = QUEUES.setdefault(pc, [])
                item = q.pop(0) if q else None
            return self._json(200, item or {"idle": True})
        if path == "/result":
            cmd_id = str(body.get("cmd_id") or "")
            with LOCK:
                RESULTS[cmd_id] = {
                    "status": "done",
                    "output": str(body.get("output") or ""),
                    "ts": time.time(),
                }
            return self._json(200, {"ok": True})
        if path == "/submit":
            pc = str(body.get("pc") or "pc")
            cmd = str(body.get("cmd") or "")
            text = str(body.get("text") or "")
            if cmd not in ALLOWED:
                return self._json(400, {"ok": False, "error": f"허용 안 된 명령: {cmd!r}"})
            if cmd in DANGEROUS and not body.get("confirm"):
                return self._json(400, {"ok": False, "error": f"{cmd} 는 confirm=true 필요"})
            cmd_id = uuid.uuid4().hex[:12]
            with LOCK:
                QUEUES.setdefault(pc, []).append({"cmd_id": cmd_id, "cmd": cmd, "text": text})
                RESULTS[cmd_id] = {"status": "pending", "output": "", "ts": time.time()}
            return self._json(200, {"ok": True, "cmd_id": cmd_id})
        return self._json(404, {"ok": False, "error": f"경로 없음: {path}"})

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") == "/status":
            if self._token_from_query() != TOKEN:
                return self._json(401, {"ok": False, "error": "토큰 불일치"})
            with LOCK:
                now = time.time()
                pcs = {p: f"{int(now - t)}초 전" for p, t in LAST_POLL.items()}
            return self._json(200, {"ok": True, "last_poll": pcs or "폴링한 PC 없음"})
        if self.path.startswith("/result/"):
            cmd_id = self.path.split("?", 1)[0].rstrip("/").split("/")[-1]
            if self._token_from_query() != TOKEN:
                return self._json(401, {"ok": False, "error": "토큰 불일치"})
            with LOCK:
                res = dict(RESULTS.get(cmd_id, {"status": "unknown"}))
            return self._json(200, res)
        return self._json(404, {"ok": False, "error": "경로 없음"})

    def _token_from_query(self):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        for kv in q.split("&"):
            k, _, v = kv.partition("=")
            if k == "token":
                return v
        return ""


def main():
    port = int(os.environ.get("PCGATE_PORT", "8791"))
    # 바인딩 주소는 환경변수로 지정한다. 기본값을 0.0.0.0 으로 둔 것은 의도적이다 —
    # 이 포트는 같은 호스트의 Open WebUI(PCGATE_URL=http://127.0.0.1:8791)와
    # tailnet 의 Windows PC 가 함께 쓴다. 127.0.0.1 로만 좁히면 웹UI 쪽이 끊기고,
    # tailnet 주소로 좁히면 공인 IP 로 붙던 PC 가 끊긴다. 어느 쪽이 실제로 붙는지는
    # 로그가 없어(log_message 를 끔) 서버 쪽에서 확인할 수 없다. 그래서 기본값은
    # 그대로 두고, 좁히려면 PCGATE_HOST 를 명시적으로 지정하게 한다.
    host = os.environ.get("PCGATE_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"pcgate listening on {host}:{port} (allowed: {sorted(ALLOWED)})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
