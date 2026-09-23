"""영속 사실 메모리 — 서버/시스템 한번 파악한 사실을 파일에 저장해 두고 재사용.

- 파일 기본 위치: AGENT_MEMORY_FILE (기본 ~/.autorcode/memory.txt)
- 맨 앞 줄은 요약(사용자에게 보여줄 사실), 뒤는 상세 기록
- remember: 사실 추가(같은 내용은 대체), recall: 현재 기억 목록
"""
import logging
import os

log = logging.getLogger("agent.notes")

HEADER = "# autorcode 기억 (이전에 파악한 사실)"


def _path(cfg_path: str = "") -> str:
    p = cfg_path or os.getenv("AGENT_MEMORY_FILE", "")
    if p:
        return os.path.expanduser(p)
    return os.path.expanduser("~/.autorcode/memory.txt")


def load(cfg_path: str = "") -> str:
    """저장된 사실 전체를 텍스트로 반환 (없으면 빈 문자열)."""
    p = _path(cfg_path)
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        log.warning("메모리 읽기 실패(%s): %s", p, e)
        return ""
    return text


def append(fact: str, cfg_path: str = "") -> str:
    """사실 한 줄 추가. 같은 텍스트가 이미 있으면 추가하지 않고 '이미 있음' 알림."""
    if not fact or not fact.strip():
        return "[오류] 기억할 내용이 비어 있다"
    p = _path(cfg_path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    try:
        with open(p, encoding="utf-8") as f:
            existed = f.read()
    except (FileNotFoundError, OSError):
        existed = ""
    fact = fact.strip()
    if existed and fact in existed:
        return "이미 기억하고 있는 내용이다. 재확인 불필요."
    lines = existed.rstrip("\n").split("\n") if existed else []
    if not existed:
        lines = [HEADER]
    lines.append("- " + fact)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        return f"[오류] 기억 저장 실패: {e}"
    return f"기억함: {fact}"


def clear(cfg_path: str = "") -> str:
    p = _path(cfg_path)
    try:
        if os.path.exists(p):
            os.remove(p)
        return "기억 전체 삭제됨"
    except OSError as e:
        return f"[오류] 삭제 실패: {e}"