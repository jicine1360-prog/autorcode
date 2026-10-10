"""영속 사실 메모리 — 서버/시스템 한번 파악한 사실을 파일에 저장해 두고 재사용.

- 파일 기본 위치: AGENT_MEMORY_FILE (기본 ~/.autorcode/memory.txt)
- 폰 비서는 사용자(chat)별로 완전 분리: ~/.autorcode/memory.txt.<chat>
  → 각 폰의 기억은 각자 파일로, 서로 안 섞인다.
- 맨 앞 줄은 요약(사용자에게 보여줄 사실), 뒤는 상세 기록
- remember: 사실 추가(같은 내용은 대체), recall: 현재 기억 목록
"""
import logging
import os
import threading
from contextlib import contextmanager
from typing import Callable, Optional

log = logging.getLogger("agent.notes")

HEADER = "# autorcode 기억 (이전에 파악한 사실)"
SUMMARY_PREFIX = "* 요약: "
DEFAULT_MAX_LINES = 40
DEFAULT_MAX_CHARS = 4000


def _limits():
    """env 를 호출 시점에 읽는다 — 테스트에서 setUp 중 바꿀 수 있게."""
    try:
        ml = int(os.getenv("AUTORCODE_MEMORY_MAX_LINES", DEFAULT_MAX_LINES))
    except ValueError:
        ml = DEFAULT_MAX_LINES
    try:
        mc = int(os.getenv("AUTORCODE_MEMORY_MAX_CHARS", DEFAULT_MAX_CHARS))
    except ValueError:
        mc = DEFAULT_MAX_CHARS
    return ml, mc

_local = threading.local()


def chat() -> str:
    """현재 실행 스레드의 사용자(폰 chat) 구분 값. 없으면 ''(공유 공간)."""
    return getattr(_local, "chat", "") or ""


@contextmanager
def scope(c: str):
    """작업 스레드에 사용자 구분을 걸어 그동안 remember/recall 을 그 유저 기억으로."""
    prev = getattr(_local, "chat", None)
    _local.chat = str(c)
    try:
        yield
    finally:
        if prev is None:
            try:
                del _local.chat
            except (AttributeError, KeyError):
                pass
        else:
            _local.chat = prev


def _path(cfg_path: str = "") -> str:
    c = chat()
    base = os.path.expanduser(
        cfg_path or os.getenv("AGENT_MEMORY_FILE", "") or "~/.autorcode/memory.txt")
    if c:
        return f"{base}.{c}"
    return base


def load_for(c: str) -> str:
    """특정 폰(chat)의 기억을 scope 없이 꺼낸다 (AI 에 이전 요청 주입용)."""
    with scope(c):
        return load()


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
    if not existed or not lines:
        lines = [HEADER]
    lines.append("- " + fact)
    kept, trimmed = _budget(lines)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
    except OSError as e:
        return f"[오류] 기억 저장 실패: {e}"
    if trimmed:
        return f"기억함: {fact} (예산 초과로 오래된 {trimmed}줄 정리)"
    return f"기억함: {fact}"


def _budget(lines: list):
    """HEADER 유지, 항목은 최신을 남기며 MAX_LINES/MAX_CHARS 로 자른다."""
    max_lines, max_chars = _limits()
    body = lines[1:] if lines and lines[0] == HEADER else lines
    summary = [x for x in body if x.startswith(SUMMARY_PREFIX)]
    body = [x for x in body if not x.startswith(SUMMARY_PREFIX)]
    removed = 0
    while len(body) > max_lines:
        body.pop(0)
        removed += 1
    while len("\n".join([HEADER] + summary + body)) > max_chars and len(body) > 1:
        body.pop(0)
        removed += 1
    return [HEADER] + summary + body, removed


def reflect(summarize: Callable[[str], str], cfg_path: str = "") -> str:
    """반성 요약: 지금 기억 전체를 summarize(llm) 로 짧게 재요약해 '요약:' 줄로 관리한다.

    요약줄은 항상 1개(기존 요약 대체)이며, 원본 항목은 최신 몇 개만 남긴다.
    """
    text = load(cfg_path)
    if not text.strip():
        return "[반성 생략] 기억이 비어 있다"
    summary = summarize(text)
    if not summary or not summary.strip():
        return "[반성 실패] 요약 결과가 비어 있다"
    return set_summary(summary, cfg_path)


def set_summary(text: str, cfg_path: str = "") -> str:
    """요약 줄을 저장하고 원본 항목은 최신 3개만 남긴다 (agent 가 요약한 뒤 호출)."""
    if not text or not text.strip():
        return "[오류] 요약 내용이 비어 있다"
    text = text.strip().replace("\n", " ")[:800]
    p = _path(cfg_path)
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.read().rstrip("\n").split("\n")
    except FileNotFoundError:
        lines = []
    except OSError as e:
        return f"[오류] 요약 저장 실패: {e}"
    items = [x for x in lines if x.startswith("- ")][-3:]
    out = [HEADER, SUMMARY_PREFIX + text] + items
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError as e:
        return f"[오류] 요약 저장 실패: {e}"
    return f"기억 요약 갱신 ({len(text)}자)"


def stats(cfg_path: str = "") -> dict:
    """기억 크기 진단 — 항목 수/문자수/요약 존재 여부."""
    text = load(cfg_path)
    lines = text.rstrip("\n").split("\n") if text else []
    items = [x for x in lines if x.startswith("- ")]
    summary = next((x for x in lines if x.startswith(SUMMARY_PREFIX)), None)
    max_lines, max_chars = _limits()
    return {"lines": len(items), "chars": len(text), "summary": bool(summary),
            "limit_lines": max_lines, "limit_chars": max_chars}


def clear(cfg_path: str = "") -> str:
    p = _path(cfg_path)
    try:
        if os.path.exists(p):
            os.remove(p)
        return "기억 전체 삭제됨"
    except OSError as e:
        return f"[오류] 삭제 실패: {e}"