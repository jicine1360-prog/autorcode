"""autorcode 웹 UI — FastAPI/WebSocket 기반의 안정적인 브라우저 프론트엔드.
"""
import argparse
import asyncio
import io
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .agent_core import Agent
from .config import load, setup_logging
from .progress import Progress, clean, result_status, short, tool_label

log = logging.getLogger("agent.webui")

_TOKEN = os.getenv("AUTORCODE_WEB_TOKEN", "")
_WORKSPACE = ""
_PERMS = "balanced"
_AUTO_YES = os.getenv("AUTORCODE_WEB_AUTO_YES", "0") == "1"
_APPROVAL_TIMEOUT = int(os.getenv("AUTORCODE_WEB_APPROVAL_TIMEOUT", "300"))

app = FastAPI(title="autorcode web")


def _token_path() -> str:
    return os.path.expanduser(os.getenv("AUTORCODE_WEB_TOKEN_FILE", "~/.autorcode/web_token"))


def _ensure_token() -> str:
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    path = _token_path()
    try:
        if os.path.isfile(path):
            _TOKEN = open(path, "r", encoding="utf-8").read().strip()
            if _TOKEN:
                return _TOKEN
    except OSError:
        pass
    _TOKEN = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_TOKEN)
    except OSError:
        log.warning("웹 토큰 파일을 쓰지 못했습니다: %s", path)
    return _TOKEN


def _authorized(token: Optional[str]) -> bool:
    return bool(_TOKEN and token and secrets.compare_digest(str(token), _TOKEN))


class WebProgress(Progress):
    def __init__(self, session: "WebSession"):
        super().__init__(enabled=True, details=False, stream=io.StringIO())
        self.session = session

    def event(self, text):
        self.session.emit({"type": "event", "text": clean(text)})

    def update(self, activity, message):
        self.session.emit({
            "type": "update",
            "label": short(activity.label, 160),
            "message": short(message, 220),
            "elapsed": round(activity.elapsed, 1),
        })

    def result(self, label, result, elapsed):
        text = clean(result)
        self.session.emit({
            "type": "result",
            "label": short(label, 180),
            "status": result_status(result),
            "elapsed": round(elapsed, 1),
            "chars": len(text),
            "preview": text[:1200],
        })

    def tool_start(self, name, args, label):
        self.session.emit({
            "type": "tool_start",
            "name": name,
            "label": short(label, 220),
            "args": short(json.dumps(args, ensure_ascii=False), 300),
        })

    def thought(self, step, text):
        self.session.emit({"type": "thought", "step": step, "text": short(text, 500)})


class WebSession:
    def __init__(self, cfg, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self.running = False
        self.approvals = {}
        self.agent: Optional[Agent] = None

    def emit(self, event: dict) -> None:
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except RuntimeError:
            pass

    def confirm(self, message: str) -> bool:
        if self.cfg.auto_yes:
            return True
        aid = uuid.uuid4().hex
        done = threading.Event()
        self.approvals[aid] = {"done": done, "ok": False}
        self.emit({"type": "approval", "id": aid, "message": clean(message)})
        ok = done.wait(timeout=_APPROVAL_TIMEOUT)
        rec = self.approvals.pop(aid, None)
        if not ok:
            self.emit({"type": "approval_timeout", "id": aid})
            return False
        return bool(rec and rec.get("ok"))

    def resolve_approval(self, aid: str, ok: bool) -> None:
        rec = self.approvals.get(aid)
        if rec:
            rec["ok"] = bool(ok)
            rec["done"].set()

    def start_prompt(self, text: str) -> None:
        if self.running:
            self.emit({"type": "error", "text": "이미 실행 중인 작업이 있습니다. 먼저 취소하세요."})
            return
        self.running = True
        self.emit({"type": "status", "running": True})

        def worker():
            try:
                answer = self.agent.run(text)
                self.emit({"type": "answer", "text": clean(answer)})
            except Exception as e:
                log.exception("웹 에이전트 실행 실패")
                self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                self.running = False
                self.emit({"type": "status", "running": False})

        threading.Thread(target=worker, name="autorcode-web-run", daemon=True).start()

    def start_compact(self) -> None:
        if self.running:
            self.emit({"type": "error", "text": "실행 중에는 요약할 수 없습니다."})
            return
        self.running = True
        self.emit({"type": "status", "running": True})

        def worker():
            try:
                summary = self.agent.compact_session()
                self.emit({"type": "answer", "text": "[요약 완료]\n" + clean(summary or "(빈 요약)")})
            except Exception as e:
                log.exception("웹 요약 실패")
                self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                self.running = False
                self.emit({"type": "status", "running": False})

        threading.Thread(target=worker, name="autorcode-web-compact", daemon=True).start()


HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>autorcode web</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; font-family: ui-sans-serif, system-ui, Apple SD Gothic Neo, Malgun Gothic, sans-serif; background:#0f0f0f; color:#eee; }
header { padding:8px 12px; border-bottom:1px solid #2a2a2a; background:#151515; }
.toprow { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.brand { font-weight:600; }
.status { color:#9b9b9b; font-size:12px; }
.menubar { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.menubar button { padding:6px 10px; border:1px solid #3a3a3a; border-radius:6px; background:#242424; color:#eee; font-size:13px; cursor:pointer; }
.menubar button:hover { background:#303030; }
.menubar button.active { background:#2f6bff; border-color:#4b84ff; }
.menubar button.danger { background:#402020; border-color:#6a2f2f; }
.menubar button.danger:hover { background:#4a2626; }
.menubar button.green { background:#1f3a25; border-color:#2f6f3f; }
main { height: calc(100vh - 156px); overflow:auto; padding:12px; }
form { position:fixed; bottom:0; left:0; right:0; display:flex; gap:8px; padding:10px; background:#151515; border-top:1px solid #2a2a2a; }
input { flex:1; padding:10px 12px; border:1px solid #3a3a3a; border-radius:8px; background:#0d0d0d; color:#eee; }
form button { padding:10px 14px; border:1px solid #2f6f3f; border-radius:8px; background:#2f6f3f; color:#fff; cursor:pointer; }
.line { margin:6px 0; padding:9px 10px; border-radius:8px; background:#1b1b1b; white-space:pre-wrap; word-break:break-word; line-height:1.6; }
.event { color:#b9b9b9; }
.answer { background:#16261b; }
.error { background:#361c1c; }
.approval { background:#33250d; }
.approval .buttons { margin-top:8px; display:flex; gap:8px; }
.approval .buttons button { padding:6px 10px; border-radius:6px; border:1px solid #444; background:#333; color:#eee; cursor:pointer; }
.approval .buttons button.ok { background:#2f6f3f; border-color:#3f8f4f; }
.approval .buttons button.no { background:#7a2f2f; border-color:#9a3f3f; }
</style>
</head>
<body>
<header>
  <div class="toprow">
    <div class="brand">autorcode web</div>
    <div class="status" id="status">연결 전</div>
  </div>
  <div class="menubar" id="menubar">
    <button onclick="sendCmd('/help')">도움말</button>
    <button onclick="sendCmd('/model')">모델</button>
    <button onclick="sendCmd('/sessions')">세션</button>
    <button onclick="sendCmd('/theme')">테마</button>
    <button onclick="sendCmd('/status')">상태</button>
    <button onclick="sendCmd('/tools')">도구</button>
    <button onclick="sendCmd('/clear')">화면지우기</button>
    <button id="btnAuto" onclick="toggleAuto()">자율모드: ?</button>
    <button id="btnApprove" onclick="toggleApprove()">전체승인: ?</button>
    <button id="btnPause" onclick="togglePause()">일시정지</button>
    <button onclick="sendCmd('/compact')">요약</button>
    <button onclick="sendCmd('/cancel')" class="danger">취소</button>
    <button onclick="sendCmd('/image')">그림</button>
    <button onclick="sendCmd('/voice')">음성</button>
    <button onclick="sendCmd('/duet')">듀엣</button>
    <button onclick="sendCmd('/copy')">복사</button>
    <button onclick="openWebSearch()">웹검색</button>
    <button onclick="sendCmd('/quit')" class="danger">종료</button>
  </div>
</header>
<main id="log"></main>
<form id="form">
  <input id="input" placeholder="작업을 입력하세요" autocomplete="off" spellcheck="false">
  <button type="submit">전송</button>
</form>
<script>
const token = localStorage.getItem('autorcode_web_token') || new URLSearchParams(location.search).get('token') || prompt('autorcode web token');
if (token) localStorage.setItem('autorcode_web_token', token);

const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const form = document.getElementById('form');
const input = document.getElementById('input');
const btnPause = document.getElementById('btnPause');
const btnAuto = document.getElementById('btnAuto');
const btnApprove = document.getElementById('btnApprove');

let ws, paused=false, autoOn=false, approveOn=false;

function add(cls, text){
  const div = document.createElement('div');
  div.className = 'line ' + cls;
  div.textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
  return div;
}
function send(obj){ if(ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function sendCmd(cmd){
  if(cmd === '/pause'){ togglePause(true); return; }
  if(cmd === '/resume'){ togglePause(false); return; }
  send({type:'prompt', text:cmd});
  input.focus();
}
function togglePause(force){
  if(force === true){ paused=false; }
  else if(force === false){ paused=true; }
  else { paused = !paused; }
  send({type: paused ? 'pause' : 'resume'});
  btnPause.textContent = paused ? '계속' : '일시정지';
  btnPause.classList.toggle('green', !paused);
  btnPause.classList.toggle('danger', paused);
}
function toggleAuto(){
  autoOn = !autoOn;
  send({type:'prompt', text: autoOn ? '/auto on' : '/auto off'});
  btnAuto.textContent = '자율모드: ' + (autoOn ? 'ON' : 'OFF');
  btnAuto.classList.toggle('active', autoOn);
}
function toggleApprove(){
  approveOn = !approveOn;
  send({type:'prompt', text: approveOn ? '/approve on' : '/approve off'});
  btnApprove.textContent = '전체승인: ' + (approveOn ? 'ON' : 'OFF');
  btnApprove.classList.toggle('active', approveOn);
}
function openWebSearch(){
  const q = prompt('웹에서 검색할 내용을 입력하세요');
  if(!q || !q.trim()) return;
  send({type:'prompt', text: '웹에서 검색해서 정리해줘: ' + q.trim()});
  add('event', '[웹검색] ' + q.trim());
}

form.onsubmit = (e)=>{
  e.preventDefault();
  const text = input.value.trim();
  if(!text) return;
  send({type:'prompt', text});
  input.value='';
};

function connect(){
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws?token=' + encodeURIComponent(token || ''));
  ws.onopen = ()=> statusEl.textContent = '연결됨';
  ws.onclose = ()=> { statusEl.textContent = '연결 끊김'; setTimeout(connect, 1500); };
  ws.onmessage = (e)=>{
    const m = JSON.parse(e.data);
    if(m.type === 'ready'){ add('event', 'ready · ' + m.model + ' · ' + m.workspace); }
    else if(m.type === 'status'){ statusEl.textContent = m.running ? '실행 중' : '대기'; }
    else if(m.type === 'event'){ add('event', m.text); }
    else if(m.type === 'thought'){ add('event', 'thought · ' + m.text); }
    else if(m.type === 'tool_start'){ add('event', 'tool · ' + m.label); }
    else if(m.type === 'result'){ add('event', 'result · ' + m.status + ' · ' + m.label + ' · ' + m.chars + '자'); }
    else if(m.type === 'answer'){ add('answer', m.text); }
    else if(m.type === 'error'){ add('error', m.text); }
    else if(m.type === 'approval'){
      const div = add('approval', '승인 필요\n' + m.message);
      const buttons = document.createElement('div');
      buttons.className = 'buttons';
      const yes = document.createElement('button'); yes.className='ok'; yes.textContent='승인';
      const no = document.createElement('button'); no.className='no'; no.textContent='거부';
      yes.onclick = ()=>{ send({type:'approve', id:m.id, ok:true}); yes.disabled=true; no.disabled=true; yes.textContent='승인됨'; };
      no.onclick = ()=>{ send({type:'approve', id:m.id, ok:false}); yes.disabled=true; no.disabled=true; no.textContent='거부됨'; };
      buttons.appendChild(yes); buttons.appendChild(no); div.appendChild(buttons);
    }else if(m.type === 'approval_timeout'){ add('error', '승인 시간 초과'); }
  };
}
connect();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    from fastapi import Response
    return Response(content=HTML, media_type="text/html; charset=utf-8", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/health")
def health():
    return {"ok": True, "workspace": _WORKSPACE, "perms": _PERMS}


async def _pump(ws: WebSocket, session: WebSession) -> None:
    while True:
        event = await session.queue.get()
        await ws.send_json(event)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    token = ws.query_params.get("token") or ws.headers.get("authorization", "").replace("Bearer ", "")
    if not _authorized(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    cfg = load()
    cfg.workspace_root = _WORKSPACE
    cfg.permissions_mode = _PERMS
    cfg.auto_yes = _AUTO_YES
    cfg.show_steps = True
    cfg.session_file = ""
    session = WebSession(cfg, loop)
    session.agent = Agent(cfg, confirmer=session.confirm, progress=WebProgress(session))
    await ws.send_json({
        "type": "ready",
        "model": cfg.model_fast,
        "workspace": cfg.workspace_root,
        "perms": cfg.permissions_mode,
    })
    pump = asyncio.create_task(_pump(ws, session))
    try:
        while True:
            msg = await ws.receive_json()
            typ = str(msg.get("type", ""))
            if typ == "prompt":
                session.start_prompt(str(msg.get("text", "")).strip())
            elif typ == "compact":
                session.start_compact()
            elif typ == "cancel":
                if session.agent:
                    session.agent.cancel()
                    session.emit({"type": "event", "text": "[요청] 취소 신호를 보냈습니다"})
            elif typ == "pause":
                if session.agent:
                    session.agent.pause()
            elif typ == "resume":
                if session.agent:
                    session.agent.resume()
            elif typ == "approve":
                session.resolve_approval(str(msg.get("id", "")), bool(msg.get("ok")))
            else:
                session.emit({"type": "error", "text": f"알 수 없는 메시지 타입: {typ}"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket 처리 실패")
    finally:
        pump.cancel()
        if session.agent:
            session.agent.cancel()
            session.agent.close()


def main():
    ap = argparse.ArgumentParser(description="autorcode web UI")
    ap.add_argument("--host", default=os.getenv("AUTORCODE_WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("AUTORCODE_WEB_PORT", "8792")))
    ap.add_argument("--workspace", default=os.getenv("AGENT_WORKSPACE", os.path.expanduser("~")))
    ap.add_argument("--perms", default=os.getenv("AGENT_PERMS", "balanced"),
                    choices=("yolo", "balanced", "strict"))
    args = ap.parse_args()

    global _WORKSPACE, _PERMS
    _WORKSPACE = os.path.realpath(args.workspace)
    if not os.path.isdir(_WORKSPACE):
        print(f"작업 폴더 없음: {_WORKSPACE}", file=sys.stderr)
        sys.exit(2)
    _PERMS = args.perms
    os.environ["AGENT_WORKSPACE"] = _WORKSPACE
    os.environ.setdefault("AGENT_PERMS", _PERMS)
    token = _ensure_token()
    setup_logging()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("웹 UI가 외부 인터페이스에 바인딩됩니다: %s", args.host)
    print(f"autorcode web: http://{args.host}:{args.port}/")
    print(f"token: {token}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
