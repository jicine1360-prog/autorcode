"""안전 계층: bash 명령 차단 목록 + 파일시스템 샌드박스.

학습용 데모지만 프로덕션 원칙을 따른다:
1. 화이트리스트(허용 목록)가 현실적으로는 어려우므로, 명시적 데인저 패턴 차단 +
   타임아웃 + 출력 상한 + 작업 디렉터리 고정을 조합한다.
2. 모든 파일 경로는 realpath 기준으로 샌드박스 루트 안에 있어야 한다.
"""
import os
import re


class UnsafeCommand(Exception):
    pass


_DENY = [
    (r"\bmkfs\b", "디스크 포맷"),
    (r"\bdd\s+if=", "디스크 직접 쓰기"),
    (r"\bdd\s+of=", "디스크 직접 쓰기(출력)"),
    (r">\s*/dev/(sd|nvme|hd|disk|mapper)", "블록장치 덮어쓰기"),
    (r":\(\)\s*\{[^}]*\|[^}]*&", "포크 폭탄"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "시스템 전원"),
    (r"\b(curl|wget)\b.*\|\s*(ba|z|k)?sh", "리모트 스크립트 파이프 실행"),
    (r"base64\s+-\w*d.*\|\s*(ba|z|k)?sh", "base64 디코드 파이프 실행"),
    (r"\bperl\s+(-\w+\s+)?-e\b", "perl -e 실행"),
    (r"python\w*\s+-(c|m|P)\b.*\b(rmtree|(os|subprocess|pathlib)\.system|shell=True)\b",
     "python 인라인 실행 탈출"),
    (r"\b(sudo|doas)\b", "권한 상승"),
    (r"^\s*su\s", "su 실행"),
    (r"\bcrontab\b\s+-r", "크론 삭제"),
    (r"/etc/(passwd|shadow|sudoers)", "시스템 인증파일"),
    (r">\s*[~$]?[A-Za-z0-9_/.-]*\.bash(rc|_profile)|>\s*[~$]?[A-Za-z0-9_/.-]*\.zshrc", "셸 rc 변조"),
]
_COMPILED = [(re.compile(p, re.I), why) for p, why in _DENY]


def check_bash(cmd: str) -> None:
    cmd = (cmd or "").strip()
    if not cmd:
        raise UnsafeCommand("빈 명령")
    for pat, why in _COMPILED:
        if pat.search(cmd):
            raise UnsafeCommand(f"차단됨({why}): {cmd[:100]}")

    # rm: 강제/재귀 삭제 대상이 절대경로·홈·상위경로면 차단 (상대경로 빌드디렉터리 삭제는 허용)
    for seg in re.split(r"&&|;|\|", cmd):
        seg = seg.strip()
        if not re.match(r"^rm(\s|$)", seg):
            continue
        args = seg.split()[1:]
        flags = " ".join(a for a in args if a.startswith("-"))
        targets = [a for a in args if not a.startswith("-")]
        forced_recursive = ("f" in flags) and ("r" in flags or "R" in flags)
        for tgt in targets:
            if tgt in ("/", "/*", "~", "$HOME", ".") :
                raise UnsafeCommand(f"위험한 삭제 대상: {tgt}")
            if forced_recursive and (tgt.startswith("/") or tgt.startswith("~")
                                     or tgt.startswith("$HOME") or ".." in tgt.split("/")):
                raise UnsafeCommand(f"위험한 강제 삭제: {tgt}")


def confine(path: str, root: str, for_write: bool = False) -> str:
    """샌드박스 루트 안의 절대경로 반환. 밖이면 UnsafeCommand."""
    root = os.path.realpath(root)
    if "$" in (path or ""):
        raise UnsafeCommand("경로에 환경변수($HOME 등) 참조 금지 — 절대경로나 ~를 사용하라")
    path = os.path.expanduser(path or ".")
    joined = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(joined)
    if real != root and not real.startswith(root + os.sep):
        raise UnsafeCommand(f"샌드박스 밖 접근 거부: {path} (루트: {root})")
    if for_write and real == root:
        raise UnsafeCommand("작업 루트 자체를 쓸 수 없음")
    return real
