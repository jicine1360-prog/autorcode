"""장기 목표(goal) — "이번 달 살 빼기" 같은 목표를 폰별로 저장하고 정기 점검한다.

- 파일: ~/.autorcode/goals.json.<chat>  (AUTORCODE_GOALS_DIR 로 위치 변경 가능)
- scan_due(): next_check 를 지난 활성 목표를 돌려준다. 봇 스케줄러가 매일 점검 메시지로 보낸다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from . import schedule as sched

log = logging.getLogger("agent.goals")

BASE = "~/.autorcode"
CHECK_EVERY = 86400  # 1일
MAX_GOALS = 12


def _path(chat: object) -> str:
    base = os.getenv("AUTORCODE_GOALS_DIR", BASE)
    return os.path.expanduser(f"{base}/goals.json.{str(chat)}")


def _load(chat: object) -> list:
    try:
        with open(_path(chat), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(chat: object, goals: list) -> None:
    os.makedirs(os.path.dirname(_path(chat)), exist_ok=True)
    with open(_path(chat), "w", encoding="utf-8") as f:
        json.dump(goals, f, ensure_ascii=False, indent=1)


def _find(goals: list, key: str):
    key = str(key).strip()
    for g in goals:
        if g.get("key") == key or key in str(g.get("goal", "")):
            return g
    return None


def add(chat: object, goal: str, steps: Optional[list] = None) -> str:
    goal = str(goal or "").strip()
    if not goal:
        return "[오류] 목표 내용이 비어 있다"
    goals = _load(chat)
    key = f"g{int(time.time()) % 100000}"
    goals.append({"key": key, "goal": goal, "status": "active",
                  "steps": [{"text": str(s), "done": False} for s in (steps or [])],
                  "created": time.time(), "next_check": time.time() + CHECK_EVERY})
    while len(goals) > MAX_GOALS:
        goals.pop(0)
    _save(chat, goals)
    return f"목표 등록({key}): {goal}"


def list_goals(chat: object) -> str:
    goals = _load(chat)
    if not goals:
        return "[목표 없음]"
    lines = []
    for g in goals:
        steps = g.get("steps") or []
        done = sum(1 for s in steps if s.get("done"))
        mark = "✅" if g.get("status") == "done" else "🎯"
        lines.append(f"{mark} [{g['key']}] {g['goal']} ({done}/{len(steps)}단계)")
    return "\n".join(lines)


def step_done(chat: object, key: str, step: str = "") -> str:
    """단계 완료 표시. step 은 번호(1부터) 또는 텍스트 일부. 없으면 첫 미완료 단계."""
    goals = _load(chat)
    g = _find(goals, key)
    if not g:
        return f"[오류] 목표를 찾지 못했습니다: {key!r} (list 로 확인)"
    steps = g.get("steps") or []
    if not steps:
        g["status"] = "done"
        _save(chat, goals)
        return f"목표 완료: {g['goal']}"
    idx = None
    sel = str(step).strip()
    if sel.isdigit():
        idx = int(sel) - 1
    elif sel:
        for i, s in enumerate(steps):
            if sel in s.get("text", ""):
                idx = i
                break
    else:
        idx = next((i for i, s in enumerate(steps) if not s.get("done")), None)
    if idx is None or not 0 <= idx < len(steps):
        return "[오류] 단계 지정이 잘못됐다"
    steps[idx]["done"] = True
    if all(s.get("done") for s in steps):
        g["status"] = "done"
        _save(chat, goals)
        return f"목표 완료: {g['goal']}"
    _save(chat, goals)
    return f"단계 완료: {steps[idx]['text']} ({sum(s.get('done') for s in steps)}/{len(steps)})"


def remove(chat: object, key: str) -> str:
    goals = _load(chat)
    g = _find(goals, key)
    if not g:
        return f"[오류] 목표를 찾지 못했습니다: {key!r}"
    goals.remove(g)
    _save(chat, goals)
    return f"목표 삭제: {g['goal']}"


def scan_due(dir_base: str, now: Optional[float] = None):
    """(chat, 점검문장) 을 next_check 지난 활성 목표에 대해 돌려준다."""
    base = os.path.expanduser(dir_base)
    now = now if now is not None else time.time()
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for n in names:
        if not n.startswith("goals.json."):
            continue
        chat = n.split("goals.json.", 1)[1]
        try:
            goals = _load(chat)
        except Exception:
            continue
        changed = False
        for g in goals:
            if g.get("status") != "active":
                continue
            if float(g.get("next_check") or 0) > now:
                continue
            steps = g.get("steps") or []
            done = sum(1 for s in steps if s.get("done"))
            when = sched.now_seoul().strftime("%H:%M")
            if steps:
                text = (f"목표 점검 {when}: [{g['key']}] {g['goal']} — "
                        f"단계 {done}/{len(steps)} 완료. 진척 알려줘?")
            else:
                text = f"목표 점검 {when}: {g['goal']} — 진행 어때?"
            out.append((int(chat) if chat.isdigit() else chat, text))
            g["next_check"] = now + CHECK_EVERY
            changed = True
        if changed:
            try:
                _save(chat, goals)
            except OSError as e:
                log.warning("goals 저장 실패(%s): %s", chat, e)
    return out