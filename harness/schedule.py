"""스케줄 관리 — 폰(chat)별 일정 저장/조회/삭제 + 정해진 시각 알림.

표준 라이브러리만 쓴다. 데이터는 <workspace>/schedule/<chat>.json (폰별 분리).

- 시간은 서울(KST, UTC+9) 기준으로 표시·해석한다(가족 대상).
- 알림: 시작 5분 전부터 시작 시각까지 딱 한 번 봇이 send 된다(scan_due→스케줄러).
"""
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

log = logging.getLogger("agent.schedule")

KST = timezone(timedelta(hours=9))
NOTIFY_BEFORE_MIN = 5
EXPIRED_AFTER_HOURS = 1

_local = threading.local()


def chat() -> str:
    return getattr(_local, "chat", "") or ""


@contextmanager
def scope(c: str):
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


def now_seoul() -> datetime:
    return datetime.now(KST)


def _file(root: str) -> str:
    c = chat()
    if not c:
        return ""
    d = os.path.join(os.path.expanduser(root or "~"), "schedule")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{c}.json")


def _to_dt(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # naive 입력 = 이미 서울시간(KST) 계약 (도구 설명과 일치)
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def fmt_event(e) -> str:
    t0 = _to_dt(e.get("start") or now_seoul())
    s = t0.strftime("%m월 %d일 %H:%M")
    if e.get("end"):
        s += f" ~ {_to_dt(e['end']).strftime('%H:%M')}"
    note = e.get("note") or ""
    return f"{s} {e['title']}" + (f" ({note})" if note else "")


def add(title: str, start=None, end=None, note="", root: str = "") -> str:
    p = _file(root)
    if not p:
        return "[오류] 폰 비서 전용 도구 (chat 구분 없음)"
    title = (title or "").strip()
    if not title:
        return "[오류] 일정 제목이 비어 있다"
    try:
        t0 = _to_dt(start) if start else now_seoul()
    except Exception:
        t0 = now_seoul()
    t1 = _to_dt(end) if end else None
    try:
        rows = _load(p)
    except Exception:
        rows = []
    key = uuid.uuid4().hex[:8]
    rows.append({"key": key, "title": title, "start": t0.isoformat(),
                 "end": t1.isoformat() if t1 else None,
                 "note": (note or "").strip(), "notified": False})
    _save(p, rows)
    return f"일정 잡았습니다: {fmt_event(rows[-1])}"


def list_events(start=None, end=None, root: str = "") -> str:
    p = _file(root)
    if not p:
        return "[오류] 폰 비서 전용 도구 (chat 구분 없음)"
    rows = _load(p)
    today = now_seoul().date()
    t0 = _to_dt(start) if start else datetime.combine(today, datetime.min.time(), KST)
    t1 = _to_dt(end) if end else datetime.combine(
        today + timedelta(days=1), datetime.min.time(), KST)
    if not rows:
        return "[일정 없음] 저장된 일정이 아직 없다."
    keep = [e for e in rows if _to_dt(e.get("start") or t0) < t1
            and _to_dt(e.get("start") or t0) >= t0]
    if not keep:
        return "해당 범위에 일정이 없습니다."
    keep.sort(key=lambda e: _to_dt(e.get("start") or t0))
    return "\n".join(f"- {fmt_event(e)} [key:{e['key']}]" for e in keep)


def remove(key: str, root: str = "") -> str:
    p = _file(root)
    if not p:
        return "[오류] 폰 비서 전용 도구 (chat 구분 없음)"
    rows = _load(p)
    kept = [e for e in rows if e.get("key") != (key or "").strip()]
    if len(kept) == len(rows):
        return "그 key 의 일정을 못 찾았습니다."
    _save(p, kept)
    return "일정 삭제했습니다."


def _load(p: str) -> list:
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(p: str, rows: list) -> None:
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def scan_due(root: str) -> list:
    """알림 창([시작-5분, 시작])에 들어선 미알림 일정을 (chat, 텍스트) 목록으로.

    이미 지나간(1시간 초과) 일정은 발송 없이 notified 로 표시(재전송 방지).
    """
    root = os.path.expanduser(root or "~")
    d = os.path.join(root, "schedule")
    if not os.path.isdir(d):
        return []
    now = now_seoul()
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        chat_id = name[:-5]
        p = os.path.join(d, name)
        rows = _load(p)
        changed = False
        for e in rows:
            if e.get("notified"):
                continue
            try:
                t0 = _to_dt(e.get("start"))
            except Exception:
                e["notified"] = True
                changed = True
                continue
            delta = (t0 - now).total_seconds()
            if delta > NOTIFY_BEFORE_MIN * 60:
                continue
            if delta < -EXPIRED_AFTER_HOURS * 3600:
                e["notified"] = True  # 지나침 — 재알림 없음
                changed = True
                continue
            e["notified"] = True
            changed = True
            out.append((int(chat_id), fmt_event(e)))
        if changed:
            _save(p, rows)
    return out