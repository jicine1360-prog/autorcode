"""작업 계획 목록(todo_write) — Claude Code 의 TodoWrite 같은 진행 트래커.

에이전트가 여러 단계 작업의 계획을 목록으로 만들고 단계마다 상태를 갱신한다.
저장소는 작업공간별(세션 파일과 같은 해시 규칙) `~/.autorcode/todo_<hash>.json`
이라 프로세스 재시작·세션 재개 후에도 계획이 살아남고, 새 작업 시작 때
미완료 계획이 컨텍스트에 다시 실린다.

상태 값: pending(대기) / doing(진행 중, 하나만) / done(완료).
"""
import hashlib
import json
import os
import time

STATUSES = ("pending", "doing", "done")
_ALIASES = {
    "": "pending", "pending": "pending", "todo": "pending", "wait": "pending",
    "doing": "doing", "in_progress": "doing", "in-progress": "doing",
    "active": "doing", "progress": "doing",
    "done": "done", "completed": "done", "complete": "done", "finished": "done",
}
_MARK = {"pending": "☐", "doing": "▶", "done": "☑"}
MAX_ITEMS = 40
MAX_CONTENT = 200
MAX_NOTE = 500


def store_path(root: str) -> str:
    h = hashlib.md5(os.path.realpath(root).encode("utf-8")).hexdigest()[:10]
    return os.path.join(os.path.expanduser("~/.autorcode"), f"todo_{h}.json")


def load(root: str) -> list:
    """현재 계획 목록([{content,status}, ...]). 없으면 빈 목록."""
    try:
        with open(store_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    items = data.get("todos") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("content")]


def load_note(root: str) -> str:
    try:
        with open(store_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return str(data.get("note") or "") if isinstance(data, dict) else ""


def save(root: str, items: list, note: str = "") -> None:
    path = store_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"todos": items, "note": note, "updated": time.time()},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)


def clear(root: str) -> None:
    try:
        os.remove(store_path(root))
    except OSError:
        pass


def has_open(root: str) -> bool:
    """미완료(pending/doing) 단계가 남아 있으면 True."""
    return any(str(i.get("status") or "pending") != "done" for i in load(root))


def normalize(raw) -> tuple:
    """모델이 준 todos 를 [{content,status}] 로 정규화. (items, warnings) 반환.

    상태 별칭 수용(in_progress/completed 등), doing 이 여러 개면 첫 개만
    doing 으로 두고 나머지는 pending 으로 내린다(로컬 모델의 흔한 실수).
    """
    warnings = []
    if not isinstance(raw, list):
        return [], [f"todos 는 목록이어야 합니다 (받은 것: {type(raw).__name__})"]
    items, doing_seen = [], False
    for entry in raw[:MAX_ITEMS]:
        if isinstance(entry, str):
            entry = {"content": entry}
        if not isinstance(entry, dict):
            warnings.append(f"{type(entry).__name__} 항목은 건너뛰었습니다")
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        if len(content) > MAX_CONTENT:
            content = content[:MAX_CONTENT] + "…"
        raw_status = str(entry.get("status") or "").strip().lower()
        if raw_status in _ALIASES:
            status = _ALIASES[raw_status]
        elif raw_status in STATUSES:
            status = raw_status
        else:
            status = "pending"
            if raw_status:
                warnings.append(f"알 수 없는 상태 '{entry.get('status')}' → pending 으로 바꿈")
        if status == "doing":
            if doing_seen:
                warnings.append("doing 단계가 2개 이상이라 뒤에 것은 pending 으로 내렸습니다")
                status = "pending"
            doing_seen = True
        items.append({"content": content, "status": status})
    if len(raw) > MAX_ITEMS:
        warnings.append(f"항목이 {len(raw)}개라 최신 {MAX_ITEMS}개만 보관했습니다")
    return items, warnings


def render(items: list, note: str = "") -> str:
    """체크리스트 텍스트. 예: [1/3 완료] / ☐ ☑ ▶"""
    if not items:
        return "(계획 목록 없음)"
    done = sum(1 for i in items if i.get("status") == "done")
    lines = [f"[{done}/{len(items)} 완료]"]
    for n, item in enumerate(items, 1):
        mark = _MARK.get(str(item.get("status") or "pending"), "☐")
        lines.append(f"{mark} {n}. {item.get('content', '')}")
    if note:
        lines.append(f"메모: {note}")
    return "\n".join(lines)


def write(root: str, todos_raw, note: str = "") -> str:
    """전체 교체 저장 후 렌더된 체크리스트(+경고)를 반환. 도구가 그대로 돌려준다."""
    note = str(note or "").strip()[:MAX_NOTE]
    if todos_raw is None or (isinstance(todos_raw, list) and not todos_raw):
        clear(root)
        return "계획 목록을 비웠습니다."
    items, warnings = normalize(todos_raw)
    if not items:
        return ("[오류] 유효한 항목이 없습니다. 형식: "
                'todos=[{"content":"단계 설명","status":"pending|doing|done"}, ...]')
    save(root, items, note)
    text = render(items, note)
    if warnings:
        text += "\n[정리] " + " · ".join(warnings)
    return text
