"""폰별 사용자 프로필 — 답변/번역에 쓸 언어 같은 개인 설정.

- 파일: ~/.autorcode/profile.json.<chat>  (AUTORCODE_PROFILE_DIR 로 위치 변경)
- language() 가 없으면 AUTORCODE_USER_LANG(기본 '한국어') 를 쓴다.
- 가족 중 영어/일본어 사용자가 있어도 폰마다 따로 기억한다.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("agent.profile")

BASE = "~/.autorcode"
DEFAULT_LANG = os.getenv("AUTORCODE_USER_LANG", "한국어")

# 이름 → 저장 코드 (이걸로 말하면 설정)
LANGS = {
    "한국어": "한국어", "한": "한국어", "korean": "한국어", "ko": "한국어",
    "영어": "영어", "영": "영어", "english": "영어", "en": "영어",
    "일본어": "일본어", "일": "일본어", "japanese": "일본어", "ja": "일본어",
    "중국어": "중국어", "중": "중국어", "chinese": "중국어", "zh": "중국어",
    "베트남어": "베트남어", "vietnamese": "베트남어", "vi": "베트남어",
    "스페인어": "스페인어", "spanish": "스페인어", "es": "스페인어",
    "프랑스어": "프랑스어", "french": "프랑스어", "fr": "프랑스어",
    "독일어": "독일어", "german": "독일어", "de": "독일어",
    "러시아어": "러시아어", "russian": "러시아어", "ru": "러시아어",
    "태국어": "태국어", "thai": "태국어", "th": "태국어",
    "인도네시아어": "인도네시아어", "indonesian": "인도네시아어", "id": "인도네시아어",
}


def _path(chat: object) -> str:
    base = os.getenv("AUTORCODE_PROFILE_DIR", BASE)
    return os.path.expanduser(f"{base}/profile.json.{str(chat)}")


def _load(chat: object) -> dict:
    try:
        with open(_path(chat), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def language(chat: object) -> str:
    return str(_load(chat).get("language") or DEFAULT_LANG)


def resolve_lang(name: str) -> Optional[str]:
    """'영어'/'English'/'en' 같은 입력 → 표준 이름. 목록 밖도 원문 그대로 허용."""
    key = str(name or "").strip()
    return LANGS.get(key.lower()) or key or None


def set_language(chat: object, lang: str) -> str:
    std = resolve_lang(lang)
    if not std:
        avail = ", ".join(sorted({v for v in LANGS.values()}))
        return f"[오류] '{lang}' 은(는) 모르는 언어야. 지원: {avail}"
    data = _load(chat)
    data["language"] = std
    os.makedirs(os.path.dirname(_path(chat)), exist_ok=True)
    try:
        with open(_path(chat), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as e:
        return f"[오류] 저장 실패: {e}"
    return f"이 폰의 언어를 {std}(으)로 설정했어요"