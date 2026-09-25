"""텔레그램 리포트 — 서버 상태 요약을 텔레그램으로 보낸다.

환경변수 (또는 ~/.autorcode/telegram.json):
- AUTORCODE_TELEGRAM_TOKEN : 봇 토큰 (@BotFather에서 발급)
- AUTORCODE_TELEGRAM_CHAT  : 채팅방 id

토큰이 없으면 stdout으로 출력한다(로컬 리포트 대체).
"""
import json
import os
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
CONFIG_PATH = os.path.expanduser("~/.autorcode/telegram.json")
_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _conf() -> tuple:
    token = os.getenv("AUTORCODE_TELEGRAM_TOKEN", "")
    chat = os.getenv("AUTORCODE_TELEGRAM_CHAT", "")
    try:
        data = json.load(open(CONFIG_PATH, encoding="utf-8"))
        token = token or data.get("token", "")
        chat = chat or str(data.get("chat", ""))
    except Exception:
        pass
    return token, chat


def send(text: str) -> str:
    """전송. 반환: 'sent' | 'printed' | 오류문자열"""
    token, chat = _conf()
    if not token or not chat:
        print(text)
        return "printed"
    body = json.dumps({"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(_URL.format(token=token), data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return "sent"
    except Exception as e:
        return f"send 실패: {e}"


def _sh(cmd: str, n: int = 12) -> str:
    try:
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                             timeout=20).stdout.strip().splitlines()
        return "\n".join(out[:n])
    except Exception:
        return ""


def collect_status() -> str:
    """서버 상태 요약 텍스트 — 리포트 본문."""
    host = socket.gethostname()
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M (%a)")
    lines = [f"📋 autorcode 일일 리포트", f"🖥 {host} · {now}", ""]
    lines.append(_sh("df -h / | tail -1 | awk '{print \"💾 디스크: \"$3\" 사용 / \"$5\" 가득\"}'"))
    lines.append(_sh("free -h | awk '/Mem:/{print \"🧠 메모리: \"$3\" / \"$2}'"))
    gpu = _sh("nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null")
    if gpu:
        lines.append("🎮 GPU:\n" + "\n".join(f"  #{i+1} {l}" for i, l in enumerate(gpu.splitlines()[:4])))
    lines.append(_sh("curl -s -m 3 http://127.0.0.1:11434/api/tags | python3 -c 'import json,sys; d=json.load(sys.stdin); print(\"🤖 ollama 모델\", len(d[\"models\"]), \"개\")' 2>/dev/null || echo '🤖 ollama 연결 없음'"))
    svc = _sh("systemctl --user is-active autorcode-bridge-v2 autorcode-pcgate 2>/dev/null | tr '\\n' ' '")
    if svc:
        lines.append(f"🔗 서비스: {svc.strip().replace(' ', '·')}")
    lines.append(_sh("journalctl --user --since '24 hours ago' -p err --no-pager 2>/dev/null | wc -l | awk '{print \"⚠️ 오류 로그: \"$1\"건 (24h)\"}'"))
    return "\n".join(l for l in lines if l is not None)


def run_report() -> int:
    result = send(collect_status())
    if result == "sent":
        print("텔레그램 전송 완료")
    elif result == "printed":
        print("(토큰 미설정 — stdout 출력으로 대체. AUTORCODE_TELEGRAM_TOKEN/CHAT 설정 필요)")
    else:
        print(result)
        return 1
    return 0
