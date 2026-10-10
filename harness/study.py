"""미리 학습(준비) — 사용자가 '모레/내일 질문할 테니 미리 공부해놔' 식으로
요청한 주제의 정리 자료를 폰(chat)별로 저장한다.

저장은 `~/.autorcode/study.json.<chat>` (key=주제, value=정리문) 이다.
작업 프리앰블에는 '학습 자료가 준비되어 있음'이라는 힌트만 실려 각 요청에
전체 내용을 태우지 않고, 답할 때 study_recall 도구로 꺼내 쓴다.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("agent.study")

BASE = "~/.autorcode"
MAX_TOPIC = 120
MAX_BODY = 6000
MAX_TOPICS = 20


def _path(chat: object) -> str:
    base = os.getenv("AUTORCODE_STUDY_DIR", BASE)
    return os.path.expanduser(f"{base}/study.json.{str(chat)}")


def _load(chat: object) -> dict:
    try:
        with open(_path(chat), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(chat: object, data: dict) -> None:
    with open(_path(chat), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def save(chat: object, topic: str, body: str) -> str:
    """주제 학습 내용 저장(같은 주제는 대체, 오래된 것부터 삭제)."""
    topic = str(topic or "").strip()[:MAX_TOPIC]
    body = str(body or "").strip()
    if not topic:
        return "[오류] 학습 주제(제목)가 비어 있다"
    if not body:
        return "[오류] 학습 내용이 비어 있다"
    data = _load(chat)
    data[topic] = body[:MAX_BODY]
    while len(data) > MAX_TOPICS:  # 넘치면 예전 것부터 버림
        data.pop(next(iter(data)))
    _save(chat, data)
    return f"학습 자료 저장: {topic}"


def recall(chat: object, topic: Optional[str] = None) -> str:
    """저장된 학습 자료를 문장으로 돌려준다. 주제 지정 시 그 주제만."""
    data = _load(chat)
    if not data:
        return "[학습 자료 없음] 아직 저장된 학습 자료가 없다."
    if topic:
        key = str(topic).strip()
        if key not in data:
            keys = " / ".join(data)
            return f"[{key}]는 저장돼 있지 않다. 준비된 주제: {keys}"
        return f"[{key}]\n{data[key]}"
    blocks = []
    for k, v in data.items():
        blocks.append(f"[{k}]\n{v}")
    return "\n\n".join(blocks)


def hint(chat: object) -> Optional[str]:
    """준비된 학습 자료가 있으면 힌트 문장(프리앰블용), 없으면 None."""
    data = _load(chat)
    if not data:
        return None
    topics = " / ".join(data)
    return f"[이 사용자의 준비된 학습 자료] {topics} — study_recall 로 꺼내 답하라"