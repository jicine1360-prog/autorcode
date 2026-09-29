#!/usr/bin/env python3
"""pcgate — Windows PC 폴링 에이전트용 명령 게이트 (포트 8791)
PC가 /poll로 명령을 가져가고, /result로 결과를 올린다.
아우토(Open WebUI 도구)는 /submit으로 명령을 넣고 /result/<id>로 기다린다.
"""
import json
import hmac
import logging
import os
import re
import stat
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("autorcode.pcgate")
TOKEN_PATH = os.path.expanduser("~/.autorcode/pcgate.token")
try:
    with open(TOKEN_PATH, encoding="utf-8") as f:
        TOKEN = f.read().strip()
except OSError:
    TOKEN = ""

ALLOWED = {"sysinfo", "disk", "procs", "updates", "speak", "lock", "sleep", "restart", "shutdown"}
DANGEROUS = {"restart", "shutdown", "sleep"}
MAX_BODY_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 32 * 1024
MAX_QUEUE_PER_PC = 100
MAX_PC_IDS = 16
MAX_RESULTS = 2000
AUTH_FAIL_LIMIT = 12
AUTH_FAIL_WINDOW = 60

QUEUES = {}   # pc -> [{"cmd_id","cmd","text"}]
RESULTS = {}  # cmd_id -> {"status","output","ts"}
LAST_POLL = {}  # pc -> ts
LOCK = threading.Lock()
AUTH_FAILS = {}  # source IP -> [monotonic timestamps]


def _token_matches(candidate: object) -> bool:
    """Fail closed on missing token; compare without data-dependent timing."""
    return bool(TOKEN) and isinstance(candidate, str) and hmac.compare_digest(candidate, TOKEN)


def _dangerous_enabled() -> bool:
    """Destructive PC actions require an explicit server-side opt-in."""
    return os.environ.get("PCGATE_DANGEROUS_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _request_token(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Backwards compatibility for the existing Open WebUI /result client.
    return parse_qs(urlsplit(handler.path).query).get("token", [""])[0]


def _auth_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with LOCK:
        recent = [t for t in AUTH_FAILS.get(ip, []) if now - t < AUTH_FAIL_WINDOW]
        AUTH_FAILS[ip] = recent
        return len(recent) >= AUTH_FAIL_LIMIT


def _record_auth_failure(ip: str):
    now = time.monotonic()
    with LOCK:
        recent = [t for t in AUTH_FAILS.get(ip, []) if now - t < AUTH_FAIL_WINDOW]
        recent.append(now)
        AUTH_FAILS[ip] = recent


def _make_result_room() -> bool:
    """Called under LOCK; evict oldest completed results, never pending work."""
    while len(RESULTS) >= MAX_RESULTS:
        completed = [(v.get("ts", 0), k) for k, v in RESULTS.items()
                     if v.get("status") == "done"]
        if not completed:
            return False
        _, oldest = min(completed)
        RESULTS.pop(oldest, None)
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        # GET API compatibility requires a query-string token for older clients;
        # never copy it into journald/access logs.
        message = a[0] % a[1:] if a else "request"
        message = re.sub(r"(?i)(token=)[^&\s]+", r"\1[redacted]", message)
        log.info("client=%s %s", self.client_address[0], message)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ValueError("Content-Length 오류")
        if n < 0:
            raise ValueError("Content-Length 오류")
        if n > MAX_BODY_BYTES:
            raise OverflowError("요청 본문이 너무 큼 (최대 64 KiB)")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError("JSON 본문 오류") from e
        if not isinstance(body, dict):
            raise ValueError("JSON 객체가 필요함")
        return body

    def _authorized(self, candidate):
        ip = self.client_address[0]
        if _auth_rate_limited(ip):
            self._json(429, {"ok": False, "error": "인증 시도 제한. 잠시 후 다시 시도하세요."})
            log.warning("authentication rate limit client=%s", ip)
            return False
        if _token_matches(candidate):
            with LOCK:
                AUTH_FAILS.pop(ip, None)
            return True
        _record_auth_failure(ip)
        log.warning("authentication failed client=%s path=%s", ip, urlsplit(self.path).path)
        self._json(401, {"ok": False, "error": "토큰 불일치"})
        return False

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        try:
            body = self._body()
        except OverflowError as e:
            return self._json(413, {"ok": False, "error": str(e)})
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        if not self._authorized(body.get("token") or _request_token(self)):
            return
        if path == "/poll":
            pc = str(body.get("pc") or "pc")[:64]
            with LOCK:
                if pc not in QUEUES and len(QUEUES) >= MAX_PC_IDS:
                    return self._json(429, {"ok": False, "error": "PC 슬롯 한도 초과"})
                LAST_POLL[pc] = time.time()
                q = QUEUES.setdefault(pc, [])
                item = q.pop(0) if q else None
            return self._json(200, item or {"idle": True})
        if path == "/result":
            cmd_id = str(body.get("cmd_id") or "")
            with LOCK:
                if not cmd_id or RESULTS.get(cmd_id, {}).get("status") != "pending":
                    return self._json(404, {"ok": False, "error": "대기 중인 명령 ID가 아님"})
                RESULTS[cmd_id] = {
                    "status": "done",
                    "output": str(body.get("output") or "")[:MAX_OUTPUT_CHARS],
                    "ts": time.time(),
                }
            return self._json(200, {"ok": True})
        if path == "/submit":
            pc = str(body.get("pc") or "pc")[:64]
            cmd = str(body.get("cmd") or "")
            text = str(body.get("text") or "")[:4096]
            if cmd not in ALLOWED:
                return self._json(400, {"ok": False, "error": f"허용 안 된 명령: {cmd!r}"})
            if cmd in DANGEROUS and not _dangerous_enabled():
                log.warning("dangerous PC command blocked by server policy client=%s cmd=%s",
                            self.client_address[0], cmd)
                return self._json(403, {"ok": False, "error": f"{cmd} 는 서버 정책으로 비활성화됨"})
            if cmd in DANGEROUS and body.get("confirm") is not True:
                return self._json(400, {"ok": False, "error": f"{cmd} 는 confirm=true 필요"})
            cmd_id = uuid.uuid4().hex[:12]
            with LOCK:
                if pc not in QUEUES and len(QUEUES) >= MAX_PC_IDS:
                    return self._json(429, {"ok": False, "error": "PC 슬롯 한도 초과"})
                queue = QUEUES.setdefault(pc, [])
                if len(queue) >= MAX_QUEUE_PER_PC:
                    return self._json(429, {"ok": False, "error": "PC 명령 대기열이 가득 참"})
                if not _make_result_room():
                    return self._json(429, {"ok": False, "error": "명령 결과 저장 한도 초과"})
                queue.append({"cmd_id": cmd_id, "cmd": cmd, "text": text})
                RESULTS[cmd_id] = {"status": "pending", "output": "", "ts": time.time()}
            return self._json(200, {"ok": True, "cmd_id": cmd_id})
        return self._json(404, {"ok": False, "error": f"경로 없음: {path}"})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/status":
            if not self._authorized(_request_token(self)):
                return
            with LOCK:
                now = time.time()
                pcs = {p: f"{int(now - t)}초 전" for p, t in LAST_POLL.items()}
            return self._json(200, {"ok": True, "last_poll": pcs or "폴링한 PC 없음"})
        if path.startswith("/result/"):
            cmd_id = path.split("/")[-1]
            if not self._authorized(_request_token(self)):
                return
            with LOCK:
                res = dict(RESULTS.get(cmd_id, {"status": "unknown"}))
            return self._json(200, res)
        return self._json(404, {"ok": False, "error": "경로 없음"})

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        raise SystemExit(f"pcgate 시작 거부: 토큰 없음 ({TOKEN_PATH})")
    try:
        token_stat = os.stat(TOKEN_PATH)
        mode = stat.S_IMODE(token_stat.st_mode)
    except OSError as e:
        raise SystemExit(f"pcgate 시작 거부: 토큰 파일 조회 실패: {e}")
    if token_stat.st_uid != os.getuid():
        raise SystemExit("pcgate 시작 거부: 토큰 파일 소유자가 서비스 사용자와 다름")
    if mode & 0o077:
        raise SystemExit(f"pcgate 시작 거부: 토큰 파일 권한은 600이어야 함 (현재 {mode:o})")
    port = int(os.environ.get("PCGATE_PORT", "8791"))
    # 바인딩 주소는 환경변수로 지정한다. 기본값을 0.0.0.0 으로 둔 것은 의도적이다 —
    # 이 포트는 같은 호스트의 Open WebUI(PCGATE_URL=http://127.0.0.1:8791)와
    # tailnet 의 Windows PC 가 함께 쓴다. 127.0.0.1 로만 좁히면 웹UI 쪽이 끊기고,
    # tailnet 주소로 좁히면 공인 IP 로 붙던 PC 가 끊긴다. 요청 로그에 원격 IP 를
    # 남기도록 바뀌었지만, PC 접속 경로를 확인할 때까지 기본 바인딩은 유지한다.
    # 확인 후 PCGATE_HOST 를 지정해 노출 범위를 좁힐 수 있다.
    host = os.environ.get("PCGATE_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"pcgate listening on {host}:{port} (allowed: {sorted(ALLOWED)})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
