"""생활 정보 — 주소→좌표(지오코딩)·주변 장소·자동차 길 안내.

무료 공개 API만 쓴다(키 불필요, 표준 라이브러리만):
  - Nominatim : 주소/장소명 → 위경도
  - Overpass (GET ?data=) : 반경 내 장소(맛집/카페/관공서 등)
  - OSRM       : 좌표 두 개 → 자동차 경로(거리·시간)

테스트는 geo._get 을 대역으로 갈아끼워 네트워크 없이 검증한다.
"""
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("agent.geo")

UA = "autorcode-skill/1.0 (personal assistant)"
NOMINATIM = os.getenv("AUTORCODE_NOMINATIM", "https://nominatim.openstreetmap.org").rstrip("/")
OVERPASS = os.getenv("AUTORCODE_OVERPASS", "https://overpass-api.de/api/interpreter")
OSRM = os.getenv("AUTORCODE_OSRM", "https://router.project-osrm.org").rstrip("/")

KINDS = {
    "맛집": {"amenity": ["restaurant", "fast_food", "food_court"]},
    "카페": {"amenity": ["cafe"]},
    "편의점": {"shop": ["convenience"]},
    "병원": {"amenity": ["hospital"]},
    "약국": {"amenity": ["pharmacy"]},
    "은행": {"amenity": ["bank"]},
    "주유소": {"amenity": ["fuel"]},
    "주차": {"amenity": ["parking"]},
    "역": {"railway": ["station"]},
    "공원": {"leisure": ["park"]},
    "관공서": {"office": ["government"], "building": ["government"],
               "amenity": ["townhall", "police", "fire_station", "courthouse"]},
}
KIND_ALIAS = {
    "식당": "맛집", "밥집": "맛집", "음식점": "맛집", "restaurant": "맛집",
    "카페": "카페", "cafe": "카페",
    "구청": "관공서", "시청": "관공서", "정부": "관공서", "관공서": "관공서",
    "편의점": "편의점", "병원": "병원", "약국": "약국", "은행": "은행",
    "주유소": "주유소", "주차": "주차", "역": "역", "지하철": "역", "공원": "공원",
}

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]


def _overpass(query: str, timeout: int = 30):
    """Overpass 질의 — 무겁지 않게: 같은 미러 1회 재시도(429 백오프) 후 다음 미러.

    미러를 여러 개 연타하면 다 같이 과도요청(429)에 걸리므로 재시도 간격과
    시도 횟수를 엄격히 제한한다.
    """
    env = os.getenv("AUTORCODE_OVERPASS", "").strip()
    mirrors = [m for m in (([m.strip() for m in env.split(",")] if env else [])
                           + OVERPASS_MIRRORS) if m]
    last = None
    for m in list(dict.fromkeys(mirrors))[:3]:
        for attempt in range(2):
            url = f"{m.rstrip('/')}?data={urllib.parse.quote(query)}"
            try:
                return _get(url, timeout)
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429 and attempt == 0:
                    time.sleep(4)  # 과도요청 — 잠깐 쉬고 같은 서버 재시도
                    continue
                break
            except Exception as e:
                last = e
                time.sleep(1.5)
                break
        log.warning("overpass 미러 포기: %s (%s)", m, type(last).__name__)
    raise last or RuntimeError("overpass unreachable")


def _tag_clauses(tags: dict):
    """{key: [values]} → Overpass 클로즈 리스트. 같은 키는 || 정규식으로 병합(질의 단축)."""
    for key, values in tags.items():
        if len(values) == 1:
            yield f'["{key}"="{values[0]}"]'
        else:
            yield f'["{key}"~"^({"|".join(sorted(values))})$"]'


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def geocode(query: str, timeout: int = 30):
    """주소/장소명 → {lat, lon, name} (못 찾으면 None)."""
    q = urllib.parse.quote(str(query).strip())
    data = _get(f"{NOMINATIM}/search?format=jsonv2&addressdetails=0&limit=1&q={q}", timeout)
    if not data:
        return None
    return {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]),
            "name": data[0].get("display_name", "")}


def reverse(lat: float, lon: float, timeout: int = 10) -> Optional[str]:
    """좌표 → 사람이 읽는 주소(한국어). 실패하면 None."""
    url = (f"{NOMINATIM}/reverse?format=jsonv2&accept-language=ko&lat={lat}&lon={lon}")
    try:
        row = _get(url, timeout)
    except Exception as e:
        log.warning("역지오코딩 실패: %s", e)
        return None
    name = ((row or {}).get("display_name") or "").strip()
    if not name:
        return None
    parts = [p.strip() for p in reversed(name.split(",")) if p.strip()]
    if parts and parts[0].lower() in {"대한민국", "한국", "south korea",
                                      "republic of korea", "korea"}:
        parts = parts[1:]
    return " ".join(parts[:3]) if parts else name


def _hav(lat1, lon1, lat2, lon2) -> int:
    r = 6371000
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = p2 - p1; dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


def _addr(tags: dict) -> str:
    parts = []
    for k in ("addr:city", "addr:street", "addr:housenumber"):
        if tags.get(k):
            parts.append(tags[k])
    return " ".join(parts)


def nearby(kind: str, lat=None, lon=None, place="", radius=2000, limit=8, timeout=30):
    """반경 내 장소 목록 (거리순). lat/lon 없으면 place 를 지오코딩한다."""
    kind = KIND_ALIAS.get(str(kind).strip(), str(kind).strip())
    tags = KINDS.get(kind)
    if not tags:
        return f"지원 종류: {', '.join(KINDS)} (예: 맛집, 카페, 관공서, 병원, 약국, 은행, 주유소, 편의점, 주차, 역, 공원)"
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        got = geocode(place)
        if not got:
            return f"[오류] 위치를 찾지 못했습니다: {place!r}"
        lat, lon = got["lat"], got["lon"]

    statements = "".join(
        f'node{x}(around:{int(radius)},{lat},{lon});'
        f'way{x}(around:{int(radius)},{lat},{lon});'
        for x in _tag_clauses(tags))
    query = f"[out:json][timeout:25];({statements});out center {int(limit) + 5};"
    try:
        data = _overpass(query, timeout)
    except Exception as e:
        log.warning("주변 장소 조회 실패: %s", e)
        return ("[오류] 주변 장소 조회가 지금 실패했습니다(무료 OSM 서버 점검/과부하). "
                "잠시 후 다시 시도해보세요. 길 안내·검색은 계속 사용 가능합니다.")

    rows = []
    for e in data.get("elements", []):
        tags_e = e.get("tags", {}) or {}
        n = tags_e.get("name") or tags_e.get("operator") or "(이름 없음)"
        c = (e.get("center") or {}) if e.get("type") == "way" else {}
        elat = e.get("lat") or c.get("lat")
        elon = e.get("lon") or c.get("lon")
        if elat is None:
            continue
        extra = tags_e.get("cuisine") or tags_e.get("amenity") or tags_e.get("shop") or ""
        rows.append({"name": n, "dist": _hav(lat, lon, float(elat), float(elon)),
                     "addr": _addr(tags_e), "extra": extra})

    rows.sort(key=lambda r: r["dist"])
    if not rows:
        return f"{kind} 없음 — 반경 {int(radius)}m 안에서 못 찾았습니다."
    lines = [f"{kind} (반경 {int(radius)}m, {len(rows)}건):"]
    for r in rows[:limit]:
        d = f"{r['dist']}m" if r["dist"] < 1000 else f"{r['dist'] / 1000:.1f}km"
        extra = r["extra"] and f" · {r['extra']}" or ""
        addr = r["addr"] and f" · {r['addr']}" or ""
        lines.append(f"  - {r['name']}{extra}{addr} (~{d})")
    return "\n".join(lines)


def _coord(v, timeout=30):
    v = str(v).strip()
    if "," in v and not v.replace(",", "").replace(" ", "").replace(".", "").replace("-", "").isdigit():
        return None
    parts = [p for p in v.replace(" ", "").split(",") if p]
    if len(parts) == 2:
        try:
            return {"lat": float(parts[0]), "lon": float(parts[1]),
                    "name": v}
        except ValueError:
            pass
    return geocode(v, timeout)


def route(a, b, timeout=30):
    """장소명 또는 '위도,경도' 두 지점 → 자동차 경로 요약."""
    ca = _coord(a, timeout)
    cb = _coord(b, timeout)
    if not ca or not cb:
        return f"[오류] 출발/도착 중 하나를 못 찾음: {a!r} / {b!r}"
    url = (f"{OSRM}/route/v1/driving/{ca['lon']},{ca['lat']};{cb['lon']},{cb['lat']}"
           "?overview=false&steps=true")
    data = _get(url, timeout)
    if not data.get("routes"):
        return "[오류] 경로를 구하지 못했습니다."
    r = data["routes"][0]
    km = r["distance"] / 1000
    mins = int(round(r["duration"] / 60))
    steps = [s for leg in r.get("legs", []) for s in leg.get("steps", [])]
    out = [f"{ca['name']} → {cb['name']}",
           f"도로 {km:.1f} km / 약 {mins}분 (자동차)"]
    for s in steps[:4]:
        inst = (s.get("maneuver") or {}).get("instruction") or s.get("name") or ""
        if inst:
            out.append(f"  → {inst}")
    return "\n".join(out)