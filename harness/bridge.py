"""autorcode HTTP 브리지 — Open WebUI 등 외부 클라이언트가 autorcode 도구를 부르게 하는 얇은 레이어.

보안 원칙:
- 토큰 인증(헤더 Authorization: Bearer <token>, 환경변수 AUTORCODE_BRIDGE_TOKEN)
- 127.0.0.1 바인딩이 기본. 외부 노출은 reverse proxy(authelia 등) 뒤에서만.
- 모든 요청은 기존 tools.execute() 로 통과하므로 RLIMIT/화이트리스트/SSRF 가드 그대로 적용.
- 요청이 지정한 root 는 서버가 정한 workspace 밖으로 나갈 수 없다. 클라이언트가
  root 를 마음대로 지정하면 샌드박스가 통째로 무력화된다(아래 _resolve_root).
- 승인 필요 판정은 agent_core 과 같은 permissions.check_bash/check_write 를 쓴다.
  이전에는 이 판정을 아예 하지 않아 토큰 하나로 웹에서 임의 셸이 즉시 돌았다.
  게이트가 없으면 '확인'이 필요한 작업은 전부 거부한다(fail-closed).
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

from harness import permissions, safety, tools  # noqa: E402

log = logging.getLogger("agent.bridge")
_TOKEN = os.getenv("AUTORCODE_BRIDGE_TOKEN", "")
# 서버가 정한 샌드박스 루트. 요청이 root 를 지정해도 이 안에 있어야 한다.
_WORKSPACE = ""
# 웹 경로 권한 정책. agent_core 과 같은 값을 써야 두 경로의 판정이 어긋나지 않는다.
_PERMS_MODE = "balanced"
# 승인 게이트. None 이면 '확인' 필요 작업은 전부 거부(fail-closed).
_CONFIRMER = None


def _gate(name: str, args: dict):
    """브리지 경유 도구 호출에 대한 권한 판정. None=실행, str=사유와 함께 거부.

    agent_core.Agent._gate 와 같은 판정을 쓴다. 다르다면 CLI 로는 막고 웹으로는
    지나는 구멍이 생긴다 — 실제로 그랬다.
    """
    verdict = None
    if name == "bash":
        verdict = permissions.check_bash(str(args.get("command", "")), _PERMS_MODE)
    elif name in ("write_file", "edit_file"):
        verdict = permissions.check_write(_PERMS_MODE)
    if not verdict:
        return None
    decision, why = verdict
    if decision == "deny":
        return f"[거부] {why}"
    if decision == "confirm":
        if _CONFIRMER is None:
            # 게이트를 못 만들었다 = 승인할 사람도 없다 = 거부. '기본 허용'으로
            # 넘어가면 bridge 는 승인 게이트가 없는 순간 전부 열린다.
            return (f"[거부] 승인 게이트가 없어 실행할 수 없습니다 ({why}). "
                    f"브리지를 재시작해 텔레그램 승인 게이트를 올리거나, "
                    f"AGENT_PERMS=balanced 로 확인이 필요 없는 작업만 쓰십시오")
        ok = _CONFIRMER(f"[웹] {name}: {json.dumps(args, ensure_ascii=False)[:200]}\n사유: {why}")
        if not ok:
            return f"[거부] 사용자 미승인 ({why})"
        log.warning("웹 경유 승인됨: %s", name)
    return None


def _resolve_root(requested: str) -> str:
    """요청이 말하는 작업 루트를, 서버가 정한 workspace 안으로만 받아들인다.

    왜 이게 필요한가: 이전에는 `req.get("root")` 를 검증 없이 그대로 샌드박스 루트로
    썼다. --workspace 를 아무리 좁혀도 클라이언트가 root 를 대신 지정하면 그대로
    샌드박스를 벗어났다. 실제로 --workspace /tmp/narrow-ws 로 띄운 브리지에
    {"root": "/home/younger"} 를 실어 보내 ~/.bashrc 를 그대로 읽었다. 토큰 하나면
    ~/.ssh/id_ed25519, ~/.autorcode/telegram.json 처럼 샌드박스 밖에 있는 비밀도
    모두 닿는다. --workspace 를 좁히는 것으로는 막을 수 없다. 요청 필드 자체가
    구멍이므로 여기서 막아야 한다.
    """
    requested = (requested or "").strip()
    if not requested:
        return _WORKSPACE
    try:
        return safety.confine(requested, _WORKSPACE)
    except safety.UnsafeCommand as e:
        raise ValueError(str(e)) from None


ALLOW_LIST = {"web_search", "web_fetch", "youtube", "read_file", "list_dir",
              "grep_files", "bash", "write_file", "edit_file", "system_info",
              "service_status", "process_list", "service_logs",
              "excel_summary", "excel_write", "pdf_read", "image_ocr",
              "remember", "recall", "forget", "find_files", "disk_usage"}


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
        try:
            root = _resolve_root(req.get("root", ""))
        except ValueError as e:
            # 클라이언트가 서버의 workspace 밖을 요청했다. 조용히 고치지 않고 거절한다 —
            # 고쳐서 주면 호출자는 샌드박스가 실제로 어디인지 오해한다.
            return self._json(400, {"ok": False, "error": f"허용 밖 작업 루트: {e}"})
        t0 = time.time()
        # 권한 판정을 먼저 한다. 실행 뒤에 하지 않으면 '거부'된 명령이 이미
        # 돌았다는 로그가 남는다.
        denied = _gate(name, args)
        if denied:
            log.warning("tool=%s 거부: %s", name, denied)
            return self._json(403, {"ok": False, "error": denied})
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
        env.setdefault("AGENT_WORKSPACE", _WORKSPACE)
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
            agent = Agent(cfg, confirmer=_CONFIRMER)
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
    ap.add_argument("--port", type=int, default=int(os.getenv("AUTORCODE_BRIDGE_PORT", "8788")))
    ap.add_argument("--host", default=os.getenv("AUTORCODE_BRIDGE_HOST", "127.0.0.1"))
    ap.add_argument("--workspace", default=os.getenv("AGENT_WORKSPACE", os.path.expanduser("~")))
    ap.add_argument("--perms", default=os.getenv("AGENT_PERMS", "balanced"),
                    choices=("yolo", "balanced", "strict"),
                    help="권한 정책. 기본 balanced (CLI 와 동일)")
    args = ap.parse_args()

    global _TOKEN, _PERMS_MODE
    _TOKEN = os.getenv("AUTORCODE_BRIDGE_TOKEN", "")
    if not _TOKEN:
        print("AUTORCODE_BRIDGE_TOKEN 환경변수가 비어 있음 — 서버 종료", file=sys.stderr)
        sys.exit(2)
    _PERMS_MODE = args.perms

    global _WORKSPACE
    _WORKSPACE = os.path.realpath(args.workspace)
    if not os.path.isdir(_WORKSPACE):
        print(f"작업 폴더 없음: {_WORKSPACE}", file=sys.stderr)
        sys.exit(2)
    # 하위 도구들이 읽는 env 도 같은 값이어야 한다. 과거에는 이 둘이 따로 놀아
    # 요청 root 와 샌드박스 가드가 어긋나는 경우가 있었다.
    os.environ["AGENT_WORKSPACE"] = _WORKSPACE
    os.environ.setdefault("AGENT_PERMS", _PERMS_MODE)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    # 승인 게이트. 텔레그램 설정이 없거나 깨져 있으면 None 이고, 그 상태로는
    # '확인' 필요 작업이 전부 거부된다(조용히 열지 않는다).
    global _CONFIRMER
    from harness import approve
    _CONFIRMER = approve.build_from_config()
    if _CONFIRMER is None:
        log.warning("승인 게이트 없음 — 웹 경유 bash/파일쓰기는 전부 거부됩니다")
    log.info("브리지 기동: http://%s:%d workspace=%s tools=%d perms=%s gate=%s",
             args.host, args.port, args.workspace, len(ALLOW_LIST), _PERMS_MODE,
             "on" if _CONFIRMER is not None else "off")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
