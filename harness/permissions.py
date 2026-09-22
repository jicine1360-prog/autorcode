"""권한 정책 — 블랙리스트(안전) 위에 화이트리스트 + 승인 게이트를 얹는다.

모드 (AGENT_PERMS):
- yolo      : 화이트리스트/승인 없음, safety.py 블랙리스트만 (학습용)
- balanced  : 기본값. bash 실행파일 화이트리스트 + 저술적 명령은 승인(확인) 요구
- strict    : balanced + bash 전체·파일쓰기까지 승인 요구

승인 결정값: ("allow", None) | ("confirm", 사유) | ("deny", 사유)
deny는 모델에게 오류로 환류되어 스스로 경로를 바꿀 수 있게 힌트를 담는다.
"""
import os
import re

ALLOW_BIN = {
    "ls", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "grep",
    "rg", "find", "pwd", "date", "echo", "printf", "df", "du", "ps", "stat",
    "file", "diff", "which", "whoami", "id", "uname", "uptime", "free", "tree",
    "env", "sed", "awk", "basename", "dirname", "seq", "true", "false", "test",
}
CONFIRM_BIN = {
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "tar",
    "zip", "unzip", "git", "python", "python3", "node", "npm", "npx", "pip",
    "pip3", "make", "cargo", "kill", "pkill", "nohup", "systemctl", "curl", "wget",
}
_PIPE_SPLIT = re.compile(r"&&|\|\||;|\||\$\(")

# 화이트리스트 바이너리라도 이 인자/패턴은 임의 실행 탈출구다
_RISKY_ARGS = [
    (re.compile(r"\bfind\b.*(?:-exec(?:dir)?|-delete|-ok)\b", re.I), "find -exec/-delete"),
    (re.compile(r"\bsed\b.*s/[^/]*/[^/]*/[a-zA-Z]*e\b", re.I), "sed /e 플래그(명령 실행)"),
    (re.compile(r"\bawk\b.*\bsystem\s*\(", re.I), "awk system()"),
    (re.compile(r"\bgit\b\s+\S*\s*-c\b.*core\.(pager|editor|sshCommand)", re.I), "git -c 실행설정"),
    (re.compile(r"\b(tar|zip)\b.*--use-(gzip|compress|checkpoint)-command", re.I), "아카이브 커맨드 플래그"),
]

# 허용 바이너리라도 이 경로 참조는 읽기/쓰기 모두 거부 (bash 명령 문자열 스캔)
_SENSITIVE = re.compile(
    r"(\.ssh/|\.ssh\b|id_rsa|id_ecdsa|id_ed25519|authorized_keys|\.aws/|\.gnupg/|"
    r"\.config/gcloud|\.config/gh|\.azure/|\.netrc|\.npmrc|/etc/(shadow|sudoers)|"
    r"\.kube/config|service.?account|secrets?manager)", re.I)


def check_bash(cmd: str, mode: str):
    if mode == "yolo":
        return "allow", None
    if _SENSITIVE.search(cmd):
        return ("deny",
                "민감 경로/자격증명 참조가 감지됨(.ssh, .aws, .netrc 등) — 이 하네스는 "
                "bash에서 자격증명 파일 접근을 의도적으로 거부한다")
    for pat, why in _RISKY_ARGS:
        if pat.search(cmd):
            return "deny", f"화이트리스트 바이너리의 실행 탈출구 차단: {why}"
    tokens0 = [s.strip() for s in _PIPE_SPLIT.split(cmd) if s.strip()]
    if not tokens0:
        return "allow", None
    firsts = [os.path.basename(s.split()[0]) for s in tokens0]
    if mode == "strict":
        return "confirm", "strict 모드: bash 전체 승인 필요"
    for b in firsts:
        if b in CONFIRM_BIN:
            return "confirm", f"저술적 명령 '{b}' 승인 필요"
        if b not in ALLOW_BIN:
            return ("deny",
                    f"'{b}'는 화이트리스트 밖 — 허용목록(읽기): {', '.join(sorted(list(ALLOW_BIN)[:12]))}… "
                    f"필요하면 저술적 도구(read_file/write_file/edit_file)나 승인모드(--yes)를 사용하라")
    return "allow", None


def check_write(mode: str):
    if mode == "strict":
        return "confirm", "strict 모드: 파일쓰기 승인 필요"
    return "allow", None
