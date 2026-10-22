"""실패 루프 감지 — '인자만 살짝 바꾼 같은 시도'를 묶어서 본다.

모델은 실패한 명령을 그대로 다시 내지 않고 self137.json → self138.json,
rs147.log → rs148.log 처럼 카운터만 바꿔 재시도하는 경우가 있다. 기존
(도구, 인자) 완전 일치 감지는 이런 루프를 놓친다.

특히 setsid/nohup/& 로 분리한 작업은 바깥 셸이 exit=0 을 돌려주기 때문에
"실패" 신호가 사라진다. 여기서는 종료코드뿐 아니라 출력의 실제 오류 문구
(rsync error code 255, ssh: connect ... 등)까지 읽어 실패로 인정한다.

제공:
  - normalize/signature : 숫자·임시 접미사를 지운 '시도 서명'
  - failed             : exit=0 이어도 출력에 오류가 있으면 실패
  - self_copy_hint     : ssh 로 들어간 호스트 안에서 자기 자신을 rsync/scp → cp 안내
  - background_hint    : setsid/nohup/& 는 exit code가 실제 결과가 아님을 안내
  - LoopGuard          : 같은 실패가 연속되면 교정 안내 → 중단
"""
import json
import re
import shlex
import socket

_DIGITS = re.compile(r"\d+")
_EXIT = re.compile(r"\[exit=(-?\d+)\]")

# exit=0 이어도 이 문구가 출력에 있으면 실패로 본다. (detached 실행의 함정)
_FAIL_MARKERS = (
    "rsync error",
    "rsync: connection unexpectedly closed",
    "traceback (most recent call last)",
    "no such file or directory",
    "permission denied",
    "connection refused",
    "connection timed out",
    "ssh: connect to host",
    "lost connection",
    "command not found",
    "code 255",
    "[timeout]",
    "[오류]",
)

# ssh 옵션 중 값을 따로 받는 것들 (target 을 잘못 집지 않도록)
_SSH_VALUE_OPTS = {
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}


def normalize(text: str) -> str:
    """휘발성 토큰(숫자)을 지워 같은 시도끼리 비교할 수 있게 만든다."""
    s = _DIGITS.sub("#", str(text or ""))
    return re.sub(r"\s+", " ", s).strip()


def signature(tool: str, args: dict) -> str:
    """(도구, 인자)의 의미 서명 — self137.json 과 self138.json 이 같아진다."""
    if str(tool) == "bash":
        return "bash:" + normalize(str((args or {}).get("command", "")))
    return f"{tool}:" + normalize(json.dumps(args or {}, ensure_ascii=False, sort_keys=True))


def failed(result: str) -> bool:
    """도구 결과가 실패인가. exit=0 이어도 출력의 오류 문구를 신뢰한다."""
    s = str(result or "")
    m = _EXIT.search(s)
    if m:
        try:
            if int(m.group(1)) != 0:
                return True
        except ValueError:
            pass
    low = s.lower()
    return any(mark in low for mark in _FAIL_MARKERS)


def _tokens(text: str) -> list:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return str(text or "").split()


def _ssh_target(command: str) -> str:
    """명령에서 ssh 접속 대상(host)을 뽑는다. 없으면 빈 문자열."""
    for m in re.finditer(r"(?<![\w.-])ssh(?:\s|$)", command):
        rest = re.split(r"[;&|\n]", command[m.end():], maxsplit=1)[0]
        toks = _tokens(rest)
        i = 0
        while i < len(toks):
            tok = toks[i]
            if tok in _SSH_VALUE_OPTS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            return tok
    return ""


def _host_of(target: str) -> str:
    t = str(target or "").strip().strip("\"'")
    if not t:
        return ""
    host = t.rsplit("@", 1)[-1]
    return host.strip("[]").lower()


def _local_aliases() -> set:
    try:
        name = socket.gethostname().lower()
    except OSError:
        name = ""
    out = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if name:
        out.add(name)
    return out


def _dest_after(command: str, pos: int) -> tuple:
    """rsync/scp 의 원격 source 뒤 목적지. ('local', path) | ('remote', '') | ('none', '')."""
    for raw in _tokens(command[pos:]):
        tok = raw.strip().strip("\"'")
        if not tok or tok in (">", ">>", "&", "|", "2>&1"):
            continue
        if tok.startswith(("-", ">", "|", "<")):
            continue
        if re.match(r"^[^/\s]+:", tok):  # host:path → 원격끼리 복사
            return "remote", ""
        if tok.startswith(("/", "~", "./", "../")) or "/" in tok:
            return "local", tok
        return "none", ""
    return "none", ""


def self_copy_hint(command: str) -> str:
    """ssh 로 들어간 호스트 안에서 그 호스트 자신을 rsync/scp 하면 cp 로 안내.

    예: ssh H 'rsync -e ssh H:/tmp/a /tmp/b'  →  cp /tmp/a /tmp/b
    같은 호스트로 SSH 를 다시 열면 키가 없을 때 반드시 인증 실패(code 255)한다.
    """
    cmd = str(command or "")
    if not re.search(r"\b(?:rsync|scp)\b", cmd):
        return ""

    ssh_host = _host_of(_ssh_target(cmd))
    host = ssh_host
    if ssh_host:
        pattern = re.compile(rf"(?:[\w.-]+@)?\[?{re.escape(host)}\]?:(\S+)", re.I)
    else:
        # ssh 없이 실행 중인데 대상이 명백한 로컬 별칭인 경우만
        aliases = "|".join(re.escape(a) for a in _local_aliases())
        pattern = re.compile(rf"(?:[\w.-]+@)?(?:{aliases}):(\S+)", re.I)

    m = pattern.search(cmd)
    if not m:
        return ""
    src = m.group(1).strip("\"'")
    kind, dest = _dest_after(cmd, m.end())
    if not src or kind == "remote":
        return ""
    if kind == "local":
        fix = f"`cp {src} {dest}`"
        return (f"[진단] 같은 호스트({host or 'local'}) 안의 파일 복사다. rsync/scp 를 SSH 로 "
                f"자기 자신에 연결하면 인증이 없어 code 255 로 실패한다. {fix} 로 바꿔라 "
                f"(원격 전송·ssh 불필요).")
    return (f"[진단] 같은 호스트({host or 'local'}) 안의 파일 복사다. rsync/scp/ssh 대신 cp 를 써라.")


def background_hint(command: str) -> str:
    """setsid/nohup/& 로 분리하면 exit code 가 실제 작업 결과가 아님을 알린다."""
    cmd = str(command or "")
    if not re.search(r"\b(?:setsid|nohup|disown)\b", cmd) and not re.search(r"&\s*(?:sleep|wait|$)", cmd):
        return ""
    return ("[진단] setsid/nohup/& 로 분리한 작업은 바깥 명령의 exit=0 이 실제 성공을 뜻하지 않는다. "
            "포그라운드로 실행해 실제 exit code 와 출력을 함께 확인하라.")


def diagnose(tool: str, args: dict) -> list:
    """실행 결과가 실패일 때 붙일 진단 힌트 목록."""
    if str(tool) != "bash":
        return []
    cmd = str((args or {}).get("command", ""))
    out = []
    for hint in (self_copy_hint(cmd), background_hint(cmd)):
        if hint:
            out.append(hint)
    return out


class LoopGuard:
    """같은 서명의 실패가 연속되면 교정 안내(steer) → 중단(abort).

    limit=3 이면: 1번 실패, 2번째 같은 실패에 steer, 3번째에 abort.
    사이에 다른(성공한) 호출이 끼면 연속 카운트는 초기화된다.
    """

    def __init__(self, limit: int = 3, steer_at: int = 2):
        self.limit = int(limit or 0)
        self.steer_at = max(2, int(steer_at or 2))
        self.reset()

    def reset(self) -> None:
        self._sig = ""
        self._streak = 0
        self._steered = False
        self.last_command = ""

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def observe(self, sig: str, is_failure: bool, command: str = "") -> str:
        """반환: '' (정상) | 'steer' | 'abort'."""
        if not self.enabled:
            return ""
        if not is_failure:
            self.reset()
            return ""
        if sig == self._sig:
            self._streak += 1
        else:
            self._sig, self._streak, self._steered = sig, 1, False
        self.last_command = command or self.last_command
        if self._streak >= self.limit:
            return "abort"
        if self._streak >= self.steer_at and not self._steered:
            self._steered = True
            return "steer"
        return ""
