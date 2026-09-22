"""도구 레지스트리 + 실행기. 모든 출력은 상한으로 잘리고, 오류는 예외 대신 문자열로 환류."""
import logging
import os
import subprocess
import sys
from typing import Callable, Dict

from . import safety, webtools

log = logging.getLogger("agent.tools")

Args = Dict[str, object]
ToolFn = Callable[[Args, str, int, int], str]


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit // 2
    return (text[:keep]
            + f"\n…[출력 상한 초과, {len(text) - limit}자 생략]…\n"
            + text[-keep:])


def _bash(args, root, max_output, timeout):
    cmd = str(args.get("command", ""))
    safety.check_bash(cmd)
    import signal
    from .config import load

    cfg = load()
    limits = [max(1, min(timeout, cfg.rlimit_cpu)), cfg.rlimit_mem_mb * 2**20,
              cfg.rlimit_fsize_mb * 2**20,
              _uid_threads() + 4 + cfg.rlimit_nproc if cfg.rlimit_nproc else 0]
    runner = os.path.join(os.path.dirname(__file__), "process_runner.py")
    command = [sys.executable, runner, *map(str, limits), cmd]

    def terminate(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            terminate(proc)
            out, err = proc.communicate()
            err += "\n[TIMEOUT] 명령이 타임아웃으로 프로세스그룹째 강제 종료됨"
        except BaseException:
            terminate(proc)
            proc.communicate()
            raise
    out = out or ""
    if err:
        out += ("\n" if out else "") + "[stderr]\n" + err
    return _cap(f"[exit={proc.returncode}]\n{out or '(출력 없음)'}", max_output)


def _uid_threads():
    """현재 UID의 총 스레드 수 (RLIMIT_NPROC이 실제로 검사하는 값)."""
    try:
        uid = os.getuid()
        total = 0
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                st = os.stat(f"/proc/{d}")
                if st.st_uid != uid:
                    continue
                with open(f"/proc/{d}/status") as f:
                    for line in f:
                        if line.startswith("Threads:"):
                            total += int(line.split()[1])
                            break
            except OSError:
                continue
        return total
    except OSError:
        return 0


def _read_file(args: Args, root: str, max_output: int, timeout: int) -> str:
    path = safety.confine(str(args.get("path", "")), root)
    if not os.path.isfile(path):
        return f"[오류] 파일 없음: {args.get('path')}"
    limit_lines = int(args.get("max_lines") or 400)
    with open(path, encoding="utf-8", errors="replace") as f:
        out = []
        for i, line in enumerate(f, 1):
            if i > limit_lines:
                out.append(f"…[{limit_lines}줄 이후 생략, read_file max_lines 증가 가능]…")
                break
            out.append(f"{i}: {line.rstrip()}")
    return _cap("\n".join(out), max_output)


def _write_file(args: Args, root: str, max_output: int, timeout: int) -> str:
    path = safety.confine(str(args.get("path", "")), root, for_write=True)
    content = str(args.get("content", ""))
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"저장됨: {path} ({len(content.encode('utf-8'))} bytes)"


def _edit_file(args: Args, root: str, max_output: int, timeout: int) -> str:
    path = safety.confine(str(args.get("path", "")), root, for_write=True)
    old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
    all_replace = bool(args.get("replace_all"))
    if not os.path.isfile(path):
        return f"[오류] 파일 없음: {args.get('path')}"
    if not old:
        return "[오류] old_string 필수"
    with open(path, encoding="utf-8") as f:
        text = f.read()
    n = text.count(old)
    if n == 0:
        return f"[오류] old_string 없음: {old[:80]!r}"
    if n > 1 and not all_replace:
        return f"[오류] {n}회 일치. old_string을 더 구체적으로 쓰거나 replace_all=true"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new))
    return f"수정됨: {path} ({n}곳 치환)"


def _list_dir(args: Args, root: str, max_output: int, timeout: int) -> str:
    path = safety.confine(str(args.get("path") or "."), root)
    if not os.path.isdir(path):
        return f"[오류] 디렉터리 없음: {args.get('path')}"
    entries = sorted(os.listdir(path), key=lambda e: (not os.path.isdir(os.path.join(path, e)), e))
    lines = [e + ("/" if os.path.isdir(os.path.join(path, e)) else "") for e in entries[:300]]
    if len(entries) > 300:
        lines.append(f"…[{len(entries) - 300}개 항목 생략]")
    return _cap("\n".join(lines) or "(비어있음)", max_output)


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}


def _grep_files(args: Args, root: str, max_output: int, timeout: int) -> str:
    import re
    pattern = str(args.get("pattern", ""))
    base = safety.confine(str(args.get("path") or "."), root)
    max_matches = int(args.get("max_matches") or 50)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[오류] 정규식 실패: {e}"
    hits, files = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > 2_000_000:
                    continue
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, 1):
                        if rx.search(line):
                            rel = os.path.relpath(fp, base)
                            hits.append(f"{rel}:{ln}: {line.rstrip()[:200]}")
                            files += 1
                            if files >= max_matches:
                                return _cap("\n".join(hits), max_output)
            except OSError:
                continue
    return _cap("\n".join(hits) if hits else "일치 없음", max_output)


TOOLS: Dict[str, ToolFn] = {
    "bash": _bash,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "grep_files": _grep_files,
    **webtools.TOOLS,
}

SCHEMAS = {
    "bash": "args: {command:str} — 샌드박스 셸 실행(차단패턴/타임아웃 적용)",
    "read_file": "args: {path:str, max_lines?:int} — 번호 매긴 파일 읽기",
    "write_file": "args: {path:str, content:str} — 파일 생성/전체 저장",
    "edit_file": "args: {path:str, old_string:str, new_string:str, replace_all?:bool} — 부분 치환",
    "list_dir": "args: {path?:str} — 디렉터리 목록",
    "grep_files": "args: {pattern:str, path?:str, max_matches?:int} — 정규식 내용 검색",
    **webtools.SCHEMAS,
}


def schema_text() -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in SCHEMAS.items())


def execute(name: str, args: Args, root: str, max_output: int, timeout: int) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return f"[오류] 알 수 없는 도구: {name!r} (가능: {', '.join(TOOLS)})"
    if not isinstance(args, dict):
        args = {}
    try:
        return fn(args, root, max_output, timeout)
    except safety.UnsafeCommand as e:
        log.warning("안전 차단 [%s]: %s", name, e)
        return f"[오류] 안전 정책으로 거부됨: {e}"
    except Exception as e:
        log.exception("도구 실패 [%s]", name)
        return f"[오류] {type(e).__name__}: {e}"
