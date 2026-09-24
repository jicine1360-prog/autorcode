"""autorcode HTTP 브리지 — Open WebUI 등 외부 클라이언트가 autorcode 도구를 부르게 하는 얇은 레이어.

보안 원칙:
- 토큰 인증(헤더 Authorization: Bearer <token>, 환경변수 AUTORCODE_BRIDGE_TOKEN)
- 127.0.0.1 바인딩이 기본. 외부 노출은 reverse proxy(authelia 등) 뒤에서만.
- 모든 요청은 기존 tools.execute() 로 통과하므로 RLIMIT/화이트리스트/SSRF 가드 그대로 적용.
- 로그는 요청/응답 크기만 남기고, 민감 내용은 같은 호스트에서만 읽는 파일 로그로.
"""
import argparse
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))

from harness import tools  # noqa: E402

log = logging.getLogger("agent.bridge")
_TOKEN = os.getenv("AUTORCODE_BRIDGE_TOKEN", "")

ALLOW_LIST = {"web_search", "web_fetch", "youtube", "read_file", "list_dir",
              "grep_files", "bash", "write_file", "edit_file",
              "excel_summary", "excel_write", "pdf_read", "image_ocr",
              "remember", "recall", "forget"}


class Handler(BaseHTTPRequestHandler):
    server_version = "autorcode/1.0"

    def _authed(self) -> bool:
        return _TOKEN and self.headers.get("Authorization") == f"Bearer {_TOKEN}"

    def _read_body(self, limit=1_000_000) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > limit:
            raise ValueError(f"본문 크기 이상 ({length})")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return json.loads(raw)

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        if not self._authed():
            return self._json(401, {"ok": False, "error": "인증 필요 (Authorization Bearer)"})
        try:
            req = self._read_body()
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"잘못된 요청: {e}"})
        name = str(req.get("tool", ""))
        if name not in ALLOW_LIST:
            return self._json(400, {"ok": False, "error": f"허용 안 되는 도구: {name!r} (가능: {sorted(ALLOW_LIST)})"})
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        root = req.get("root", "") or os.getenv("AGENT_WORKSPACE", "")
        if not root or not os.path.isdir(root):
            return self._json(400, {"ok": False, "error": f"작업 폴더 없음: {root!r}"})
        t0 = time.time()
        result = tools.execute(name, args, root=root,
                               max_output=int(req.get("max_output") or 8000),
                               timeout=int(req.get("timeout") or 60))
        log.info("tool=%s args_keys=%s dt=%.2fs out_len=%d",
                 name, sorted(args)[:8], time.time() - t0, len(result))
        return self._json(200, {"ok": True, "name": name, "result": result})

    def do_POST(self):
        if self.path.rstrip("/").endswith("/tools/show"):
            return self._route_show()
        if self.path.rstrip("/").endswith("/tool"):
            return self._route()
        if self.path.rstrip("/").endswith("/run"):
            return self._route_run()
        return self._json(404, {"ok": False, "error": f"경로 없음: {self.path}"})

    def _route_show(self):
        if not self._authed():
            return self._json(401, {"ok": False, "error": "인증 필요"})
        return self._json(200, {"ok": True, "tools": tools.schema_text()})

    def _route_run(self):
        if not self._authed():
            return self._json(401, {"ok": False, "error": "인증 필요"})
        try:
            req = self._read_body()
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"잘못된 요청: {e}"})
        prompt = str(req.get("prompt", "")).strip()
        if not prompt:
            return self._json(400, {"ok": False, "error": "prompt 필수"})
        # 컨테이너/외부에서 온 요청은 확인 프롬프트 없이 기본 거부 방침으로 실행.
        cfg_extra = {}
        for k in ("model_fast", "model_smart"):
            v = req.get(k)
            if isinstance(v, str) and v:
                cfg_extra[k] = v
        env = os.environ.copy()
        env.setdefault("AGENT_PROVIDER", "ollama")
        env.setdefault("AGENT_WORKSPACE", req.get("root", os.getenv("AGENT_WORKSPACE", "")))
        for k, v in cfg_extra.items():
            env[f"AGENT_{k.upper()}"] = v
        _prev = {k: os.environ.get(k) for k in (
            "AGENT_PROVIDER", "AGENT_WORKSPACE", "AGENT_MODEL_FAST", "AGENT_MODEL_SMART")}
        try:
            for k, v in env.items():
                if k.startswith("AGENT_") or k in ("AGENT_PROVIDER",):
                    os.environ[k] = v
            from .agent_core import Agent
            from .config import load

            cfg = load()
            agent = Agent(cfg, confirmer=lambda _q: False)  # 브리지 경유 = 자동승인 없음
            try:
                result = agent.run(prompt)
            finally:
                agent.close()
            return self._json(200, {"ok": True, "result": result})
        except Exception as e:
            log.exception("에이전트 실행 실패")
            return self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        finally:
            for k, v in _prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def main():
    ap = argparse.ArgumentParser(description="autorcode HTTP 브리지")
    ap.add_argument("--port", type=int, default=int(os.getenv("AUTORCODE_BRIDGE_PORT", "8787")))
    ap.add_argument("--host", default=os.getenv("AUTORCODE_BRIDGE_HOST", "127.0.0.1"))
    ap.add_argument("--workspace", default=os.getenv("AGENT_WORKSPACE", os.path.expanduser("~")))
    args = ap.parse_args()

    global _TOKEN
    _TOKEN = os.getenv("AUTORCODE_BRIDGE_TOKEN", "")
    if not _TOKEN:
        print("AUTORCODE_BRIDGE_TOKEN 환경변수가 비어 있음 — 서버 종료", file=sys.stderr)
        sys.exit(2)

    os.environ.setdefault("AGENT_WORKSPACE", args.workspace)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("브리지 기동: http://%s:%d workspace=%s tools=%d",
             args.host, args.port, args.workspace, len(ALLOW_LIST))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()