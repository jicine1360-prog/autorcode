"""쓰기 체크포인트 → 롤백. write_file/edit_file 실행 전 원본을 스냅샷하고
rollback 도구로 되돌린다. 롤백 자체는 스냅샷하지 않는다 (되돌리기 가능).

Grok Build(xai-org/grok-build, Apache-2.0)의 checkpoint/rollback 레이어에서
발상 — 구현은 autorcode 구조에 맞게 작성.
"""
import json
import os
import shutil
import time
import uuid

from . import safety

KEEP = 50  # 세션 루트당 보관할 최대 체크포인트 수


def _dir(root: str) -> str:
    return os.path.join(root, ".autorcode", "checkpoints")


def snapshot(root: str, path: str, tool: str) -> str | None:
    """쓰기 직전 원본 스냅샷. 체크포인트 id 반환. 실패해도 None만 반환하고
    쓰기 작업 자체는 막지 않는다."""
    try:
        cid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = os.path.join(_dir(root), cid)
        os.makedirs(d, exist_ok=True)
        existed = os.path.isfile(path)
        entry = {"path": path, "existed": existed, "tool": tool}
        if existed:
            shutil.copy2(path, os.path.join(d, "0"))
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"id": cid, "ns": time.time_ns(),
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "files": [entry]}, f, ensure_ascii=False)
        _prune(root)
        return cid
    except OSError:
        return None


def _prune(root: str) -> None:
    """최근 KEEP개만 보관 (정렬은 list_checkpoints와 동일하게 manifest ns 기준)."""
    try:
        cps = list_checkpoints(root)
        if len(cps) <= KEEP:
            return
        for m in cps[KEEP:]:
            shutil.rmtree(os.path.join(_dir(root), m["id"]), ignore_errors=True)
    except OSError:
        pass


def list_checkpoints(root: str) -> list[dict]:
    """보관 중인 체크포인트 (최신순)."""
    base = _dir(root)
    out = []
    try:
        for d in os.listdir(base):
            mp = os.path.join(base, d, "manifest.json")
            if os.path.isfile(mp):
                try:
                    with open(mp, encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    out.sort(key=lambda m: (m.get("ns", 0), m.get("id", "")), reverse=True)
    return out


def rollback(root: str, cid: str = "last") -> str:
    """체크포인트 시점으로 복원. 'last' 또는 id 지정. 없었던 파일은 삭제,
    있었던 파일은 백업본으로 덮어쓴다."""
    cps = list_checkpoints(root)
    if not cps:
        return "[오류] 체크포인트 없음"
    if cid in ("", "last"):
        cp = cps[0]
    else:
        cp = next((m for m in cps if m["id"] == cid), None)
        if cp is None:
            return f"[오류] 체크포인트 없음: {cid} (가능: {', '.join(m['id'] for m in cps[:10])})"
    base = os.path.join(_dir(root), cp["id"])
    restored = []
    for entry in cp["files"]:
        path = safety.confine(entry["path"], root, for_write=True)
        try:
            if entry["existed"]:
                shutil.copy2(os.path.join(base, "0"), path)
                restored.append(f"복원: {path}")
            else:
                if os.path.isfile(path):
                    os.remove(path)
                    restored.append(f"삭제: {path} (체크포인트 당시 없었음)")
                else:
                    restored.append(f"건너뜀: {path} (이미 없음)")
        except OSError as e:
            restored.append(f"[오류] {path}: {e}")
    return f"롤백 완료 [{cp['id']}]\n" + "\n".join(restored)
