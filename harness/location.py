"""폰 위치 — 사용자가 텔레그램으로 공유한 위치를 폰(chat)별로 저장/조회.

텔레그램 봇은 폰의 GPS 를 자동으로 읽지 못한다. 사용자가 '내 위치'를 공유하면
그 좌표를 `~/.autorcode/location.json.<chat>` 에 저장하고, 이후 '근처/주변'
기준으로 쓴다. 파일은 JSON 한 줄이며 폰마다 별도라 서로 안 섞인다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from . import geo

log = logging.getLogger("agent.location")

BASE = "~/.autorcode"


def _path(chat: object) -> str:
    base = os.getenv("AUTORCODE_LOCATION_DIR", BASE)
    return os.path.expanduser(f"{base}/location.json.{str(chat)}")


def store(chat: object, lat: float, lon: float, name: Optional[str] = None) -> dict:
    """좌표를 저장하고, best-effort 로 역지오코딩해 장소 이름을 채운다."""
    rec = {"lat": float(lat), "lon": float(lon), "ts": time.time()}
    if name is None:
        try:
            name = geo.reverse(rec["lat"], rec["lon"])
        except Exception:
            name = None
    rec["name"] = name or ""
    with open(_path(chat), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return rec


def load(chat: object) -> Optional[dict]:
    """저장된 위치(없으면 None)."""
    try:
        with open(_path(chat), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def describe(chat: object) -> Optional[str]:
    """문장으로 변환 — 작업 프리앰블용. 위치가 없으면 None."""
    rec = load(chat)
    if not rec:
        return None
    base = f"위도 {rec['lat']:.5f}, 경도 {rec['lon']:.5f}"
    return f"{rec['name']} ({base})" if rec.get("name") else base