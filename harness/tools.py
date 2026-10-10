"""도구 레지스트리 + 실행기. 모든 출력은 상한으로 잘리고, 오류는 예외 대신 문자열로 환류."""
import logging
import fnmatch
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Callable, Dict

from . import notes, safety, schedule, webtools
from . import geo
from . import study

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
    whole_word = bool(args.get("whole_word"))
    if not os.path.isfile(path):
        return f"[오류] 파일 없음: {args.get('path')}"
    if not old:
        return "[오류] old_string 필수"
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if whole_word:
        pat = re.compile(r"\b(?:" + re.escape(old) + r")\b")
        n = len(pat.findall(text))
        if n == 0:
            return f"[오류] whole_word 일치 없음: {old[:80]!r}"
        if n > 1 and not all_replace:
            return f"[오류] {n}회 일치. old_string을 더 구체적으로 쓰거나 replace_all=true"
        text = pat.sub(lambda _m: new, text)
    else:
        n = text.count(old)
        if n == 0:
            return f"[오류] old_string 없음: {old[:80]!r}"
        if n > 1 and not all_replace:
            return f"[오류] {n}회 일치. old_string을 더 구체적으로 쓰거나 replace_all=true"
        text = text.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
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


def _find_files(args: Args, root: str, max_output: int, timeout: int) -> str:
    """Workspace 안에서 이름 패턴을 찾는다. shell 문자열을 실행하지 않는다."""
    base = safety.confine(str(args.get("path") or "."), root)
    pattern = str(args.get("pattern") or "").strip()
    if not pattern or len(pattern) > 200:
        return "[오류] pattern은 1~200자로 지정하세요"
    try:
        max_depth = max(1, min(int(args.get("max_depth") or 4), 16))
        max_matches = max(1, min(int(args.get("max_matches") or 40), 200))
    except (TypeError, ValueError):
        return "[오류] max_depth/max_matches는 정수여야 합니다"
    if not os.path.exists(base):
        return f"[오류] 경로 없음: {args.get('path')}"
    matches = []
    pat = pattern.casefold()
    if os.path.isfile(base):
        return os.path.basename(base) if fnmatch.fnmatchcase(os.path.basename(base).casefold(), pat) else "일치 없음"
    candidates = os.walk(base, followlinks=False)
    for dirpath, dirnames, filenames in candidates:
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        if depth >= max_depth:
            dirnames[:] = []
        for name in sorted([*dirnames, *filenames]):
            if fnmatch.fnmatchcase(name.casefold(), pat):
                full = os.path.join(dirpath, name)
                out = os.path.relpath(full, base)
                if os.path.isdir(full) and not out.endswith(os.sep):
                    out += os.sep
                matches.append(out)
                if len(matches) >= max_matches:
                    return _cap("\n".join(matches) + f"\n…결과 상한 {max_matches}개", max_output)
    return _cap("\n".join(matches) if matches else "일치 없음", max_output)


def _disk_usage(args: Args, root: str, max_output: int, timeout: int) -> str:
    """Workspace 경로의 파일시스템 여유 공간과 상위 폴더 크기를 제한적으로 계산."""
    base = safety.confine(str(args.get("path") or "."), root)
    if not os.path.exists(base):
        return f"[오류] 경로 없음: {args.get('path')}"
    try:
        usage = shutil.disk_usage(base)
    except OSError as e:
        return f"[오류] 디스크 조회 실패: {e}"
    used = usage.total - usage.free
    lines = [f"[디스크] {base}: 사용 {used / 2**30:.1f}/{usage.total / 2**30:.1f} GiB "
             f"({used / usage.total * 100:.1f}%), 여유 {usage.free / 2**30:.1f} GiB"]
    if not os.path.isdir(base):
        return _cap("\n".join(lines), max_output)

    sizes: dict[str, int] = {}
    deadline = time.monotonic() + max(1, min(timeout, 15))
    budget = 15000
    visited = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        rel = os.path.relpath(dirpath, base)
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp, follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(fp):
                continue
            top = name if rel == "." else rel.split(os.sep, 1)[0]
            sizes[top] = sizes.get(top, 0) + st.st_size
            visited += 1
            if visited >= budget or time.monotonic() >= deadline:
                truncated = True
                break
        if truncated:
            break
    for name, size in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))[:30]:
        lines.append(f"  {name:<40} {size / 2**30:8.2f} GiB")
    if truncated:
        lines.append(f"  (부분 집계: {visited:,}개 파일/시간 제한; 큰 항목은 실제보다 작게 보일 수 있음)")
    return _cap("\n".join(lines), max_output)


def _service_status(args: Args, root: str, max_output: int, timeout: int) -> str:
    """현재 사용자 세션의 서비스 상태만 읽는다."""
    state = str(args.get("state") or "all").lower()
    if state not in {"all", "active", "failed", "inactive"}:
        return "[오류] state는 all/active/failed/inactive 중 하나여야 합니다"
    exe = shutil.which("systemctl")
    if not exe:
        return "[오류] systemctl 없음"
    cmd = [exe, "--user", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"]
    if state != "all":
        cmd.append(f"--state={state}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=min(timeout, 15))
    except (OSError, subprocess.SubprocessError) as e:
        return f"[오류] 서비스 조회 실패: {e}"
    if result.returncode:
        return f"[오류] systemctl: {(result.stderr or '').strip()[:300]}"
    return _cap(result.stdout.strip() or "일치하는 사용자 서비스 없음", max_output)


def _process_list(args: Args, root: str, max_output: int, timeout: int) -> str:
    """프로세스 목록 상위 CPU 소비 항목."""
    try:
        limit = max(1, min(int(args.get("limit") or 15), 30))
    except (TypeError, ValueError):
        return "[오류] limit은 정수여야 합니다"
    ps = shutil.which("ps")
    if not ps:
        return "[오류] ps 없음"
    try:
        result = subprocess.run([ps, "-eo", "pid,ppid,comm,%cpu,%mem", "--sort=-%cpu"],
                                capture_output=True, text=True, timeout=min(timeout, 10))
    except (OSError, subprocess.SubprocessError) as e:
        return f"[오류] 프로세스 조회 실패: {e}"
    if result.returncode:
        return f"[오류] ps: {(result.stderr or '').strip()[:300]}"
    return _cap("\n".join(result.stdout.splitlines()[:limit + 1]), max_output)


def _service_logs(args: Args, root: str, max_output: int, timeout: int) -> str:
    """허용된 사용자 서비스의 최근 로그. secret 패턴은 반환 전 마스킹한다."""
    service = str(args.get("service") or "")
    if not re.fullmatch(r"[A-Za-z0-9_@.-]{1,80}\.service", service) or service.startswith("-"):
        return "[오류] 서비스 이름이 올바르지 않습니다 (.service 이름 필요)"
    try:
        lines = max(1, min(int(args.get("lines") or 50), 200))
    except (TypeError, ValueError):
        return "[오류] lines는 정수여야 합니다"
    exe = shutil.which("journalctl")
    if not exe:
        return "[오류] journalctl 없음"
    try:
        result = subprocess.run([exe, "--user", "-u", service, "-n", str(lines), "--no-pager", "-o", "short-iso"],
                                capture_output=True, text=True, timeout=min(timeout, 20))
    except (OSError, subprocess.SubprocessError) as e:
        return f"[오류] journal 조회 실패: {e}"
    text = result.stdout or result.stderr or "로그 없음"
    text = re.sub(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", "[telegram-token-redacted]", text)
    text = re.sub(r"\bsk-or-v1-[A-Za-z0-9_-]{20,}\b", "[api-key-redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", r"\1[redacted]", text)
    return _cap(text.strip() or "로그 없음", max_output)


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


def _remember(args, root, max_output, timeout):
    fact = str(args.get("fact", "")).strip()
    maxlen = int(args.get("max_len") or 0)
    if maxlen and len(fact) > maxlen:
        fact = fact[:maxlen] + "…"
    return notes.append(fact)


def _recall(_args, root, max_output, timeout):
    text = notes.load()
    return text or "[기억 없음] 아직 저장된 사실이 없다."


def _forget(_args, root, max_output, timeout):
    return notes.clear()


def _memory_reflect(_args, root, max_output, timeout):
    s = notes.stats()
    text = notes.load() or "[기억 없음]"
    return (f"현재 기억: {s['lines']}줄(한도 {s['limit_lines']}), {s['chars']}자"
            f"(한도 {s['limit_chars']}), 요약줄={'있음' if s['summary'] else '없음'}\n"
            f"--- 본문 ---\n{text[:max_output - 300]}\n---\n"
            "줄이 한도에 가까우면(또는 '정리/요약해줘' 요청) 위 내용을 3~5문장 핵심으로 다시 정리해 "
            "memory_save_summary(summary=...) 로 저장하라. 개별 사실은 지우지 말고 요약에 녹여라.")


def _memory_save_summary(args, root, max_output, timeout):
    return notes.set_summary(str(args.get("summary", "")))


def _schedule_add(args, root, max_output, timeout):
    return schedule.add(args.get("title") or "", args.get("start"),
                        args.get("end"), args.get("note") or "", root)


def _schedule_list(args, root, max_output, timeout):
    return schedule.list_events(args.get("start"), args.get("end"), root)


def _schedule_remove(args, root, max_output, timeout):
    return schedule.remove(args.get("key") or "", root)


def _study_save(args, root, max_output, timeout):
    return study.save(notes.chat(), args.get("topic") or "", args.get("content") or "")


def _study_recall(args, root, max_output, timeout):
    return study.recall(notes.chat(), args.get("topic") or None)


def _geo_geocode(args, root, max_output, timeout):
    got = geo.geocode(args.get("query") or "")
    if not got:
        return "위치를 찾지 못했습니다."
    return f"{got['name']}\n위경도: {got['lat']}, {got['lon']}"


def _geo_nearby(args, root, max_output, timeout):
    return geo.nearby(args.get("kind") or "", args.get("lat"),
                      args.get("lon"), args.get("place") or "",
                      int(args.get("radius") or 2000),
                      int(args.get("limit") or 8), timeout)


def _geo_route(args, root, max_output, timeout):
    return geo.route(args.get("from") or "", args.get("to") or "", timeout)


# ---------------- 엑셀 (openpyxl) ----------------

def _excel_summary(args, root, max_output, timeout):
    path = safety.confine(str(args.get("path", "")), root)
    if not os.path.isfile(path):
        return f"[오류] 파일 없음: {args.get('path')}"
    if not path.lower().endswith(".xlsx"):
        return "[오류] .xlsx 파일만 지원"
    sheet_name = str(args.get("sheet") or "")
    try:
        from openpyxl import load_workbook
    except ImportError:
        return "[오류] openpyxl 미설치 — pip install openpyxl"
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        return f"[오류] 엑셀 열기 실패: {e}"
    if sheet_name and sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
    else:
        ws = wb[wb.sheetnames[0]]
        sheet_name = ws.title
    lines = [f"파일: {path}", f"시트: {sheet_name}  (전체 시트: {', '.join(wb.sheetnames)})"]
    max_rows = int(args.get("max_rows") or 30)
    ncol = 0
    rows_out = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i > max_rows:
            break
        cells = ["" if v is None else str(v) for v in row]
        ncol = max(ncol, len(cells))
        rows_out.append(cells)
    if not rows_out:
        return "[엑셀] 빈 시트"
    # 열 너비 조정(제목 12자, 값 18자)
    col_w = [max(12, max((len(r[j]) if j < len(r) else 0) for r in rows_out) + 2)
             for j in range(ncol)]
    header = " | ".join(h.ljust(min(40, col_w[j])) for j, h in enumerate(rows_out[0]))
    lines.append("열제목: " + header)
    lines.append(f"행수(처음 {len(rows_out)}행 / 전체는 max_rows 증가): {ws.max_row} · 열수: {ws.max_column}")
    for r in rows_out[1:]:
        lines.append(" | ".join((r[j] if j < len(r) else "")[:min(50, col_w[j]) + 0]
                                for j in range(min(len(r), ncol))))
    # 숫자 컬럼 합계 (마지막 행까지)
    sums = []
    for j in range(ncol):
        total = 0
        ok = True
        for r in ws.iter_rows(values_only=True):
            if j < len(r) and isinstance(r[j], (int, float)):
                total += r[j]
            elif j < len(r) and r[j] is not None:
                ok = False
                break
        if ok:
            sums.append(f"{rows_out[0][j] if j < len(rows_out[0]) else ''}={round(total, 4)}")
    if sums:
        lines.append("숫자합계: " + ", ".join(sums))
    wb.close()
    return _cap("\n".join(lines), max_output)


def _excel_write(args, root, max_output, timeout):
    path = str(args.get("path", ""))
    if not path:
        return "[오류] path 필수"
    if not path.lower().endswith(".xlsx"):
        path += ".xlsx"
    path = safety.confine(path, root, for_write=True)
    content = str(args.get("content", ""))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return "[오류] openpyxl 미설치"
    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    # Markdown 표 -> 시트
    rows = [ln for ln in content.splitlines() if ln.strip()]
    data_rows = []
    for ln in rows:
        if "|" in ln and not ln.strip().startswith("|---"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            data_rows.append(cells)
    if not data_rows:
        data_rows = [[ln] for ln in rows]
    for r, cells in enumerate(data_rows, 1):
        for c, val in enumerate(cells, 1):
            cell = ws.cell(row=r, column=c, value=val)
            if r == 1:
                cell.font = Font(bold=True)
    for col in ws.columns:
        width = max(len(str(c.value)) + 2 if c.value else 10 for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(width, 60)
    try:
        wb.save(path)
    except Exception as e:
        return f"[오류] 저장 실패: {e}"
    return f"저장됨: {path} ({len(data_rows)}행 × {max((len(r) for r in data_rows), default=0)}열)"


def _pdf_read(args, root, max_output, timeout):
    path = safety.confine(str(args.get("path", "")), root)
    if not os.path.isfile(path):
        return f"[오류] 파일 없음: {args.get('path')}"
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return "[오류] pdftotext (poppler-utils) 미설치"
    import tempfile
    t0 = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        tmp = tf.name
    try:
        r = subprocess.run([pdftotext, "-layout", path, tmp],
                           capture_output=True, text=True, timeout=min(timeout, 90))
        if r.returncode != 0:
            return f"[오류] pdftotext: {(r.stderr or 'unknown')[:200]}"
        with open(tmp, encoding="utf-8", errors="replace") as f:
            text = f.read()
    finally:
        os.unlink(tmp)
    n = text.count("\n")
    head = f"PDF: {os.path.basename(path)} · {n+1}행 · {time.monotonic()-t0:.1f}초\n{'-'*40}\n"
    return _cap(head + text, max_output)


def _image_ocr(args, root, max_output, timeout):
    path = safety.confine(str(args.get("path", "")), root)
    if not os.path.isfile(path):
        return f"[오류] 파일 없음: {args.get('path')}"
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return "[오류] tesseract 미설치"
    lang = str(args.get("lang") or "kor+eng")
    try:
        r = subprocess.run([tesseract, path, "stdout", "-l", lang],
                           capture_output=True, text=True, timeout=min(timeout, 120))
    except subprocess.TimeoutExpired:
        return "[오류] OCR 타임아웃 (큰 이미지?)"
    if r.returncode != 0:
        return f"[오류] tesseract: {(r.stderr or 'unknown')[:200]}"
    text = r.stdout.strip()
    if not text:
        return "[OCR] 텍스트를 찾지 못함 (흐린/회전 이미지일 수 있음)"
    return _cap(f"[OCR(사진→텍스트)] {path}\n{'-'*40}\n{text}", max_output) if len(text) > 0 else text


def _system_info(args, root, max_output, timeout):
    """지금 실제로 실행 중인 기기의 라이브 상태.

    설치 목록(dpkg/pip -l) 이 아니라 구동 중 값을 읽는다: 커널/호스트, CPU 모델과
    코어 수, RAM 사용량, 디스크 사용률, GPU 의 실시간 VRAM 사용량, 그리고 지금
    GPU 에 올라와 있는 모델. '무엇이 설치돼 있나'와 '무엇이 돌고 있나'는 다른
    질문이고, 후자가 필요해서 이 도구가 있다.
    """
    want = str(args.get("section") or "all").lower()
    secs = want if want in _SYS_SECTIONS else "all"
    out: list[str] = []
    tmo = min(timeout, 15)

    if secs in ("all", "host"):
        try:
            un = os.uname()
            out.append(f"[호스트] {socket.gethostname()}  {un.sysname} {un.release}  {un.machine}")
            boot = _boot_elapsed()
            if boot:
                out.append(f"        가동 {boot}")
        except Exception as e:
            out.append(f"[호스트] 조회 실패: {e}")

    if secs in ("all", "cpu"):
        model, cores = "(알 수 없음)", 0
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("model name") and model == "(알 수 없음)":
                        model = line.split(":", 1)[1].strip()
                    if line.startswith("processor"):
                        cores += 1
        except OSError as e:
            model = f"조회 실패: {e}"
        try:
            l1, l5, l15 = os.getloadavg()
            load = f"  부하(1/5/15분) {l1:.2f} / {l5:.2f} / {l15:.2f}"
        except OSError:
            load = ""
        out.append(f"[CPU]   {model}  ×{cores} 코어{load}")

    if secs in ("all", "mem"):
        try:
            info: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    if k in ("MemTotal", "MemAvailable", "SwapTotal"):
                        info[k] = int(v.split()[0])
            tot = info.get("MemTotal", 0) // 1024
            avail = info.get("MemAvailable", 0) // 1024
            used = tot - avail
            sw = info.get("SwapTotal", 0) // 1024
            pct = (used / tot * 100) if tot else 0
            out.append(f"[메모리] {used:,} / {tot:,} MiB 사용 ({pct:.0f}%), 스왑 {sw:,} MiB")
        except (OSError, ValueError, IndexError) as e:
            out.append(f"[메모리] 조회 실패: {e}")

    if secs in ("all", "gpu"):
        rows = _nvidia_live(tmo)
        out.append(f"[GPU]   {rows}" if rows else "[GPU]   NVIDIA GPU 없음")

    if secs in ("all", "models"):
        out.append(f"[모델]  {_live_models(tmo)}")

    if secs in ("all", "disk"):
        for target in ("/", os.path.expanduser("~")):
            try:
                u = shutil.disk_usage(target)
                used = u.total - u.free
                out.append(f"[디스크] {target:<12} {used / u.total * 100:5.1f}% 사용, "
                           f"여유 {u.free / 2**30:,.0f} GiB / {u.total / 2**30:,.0f} GiB")
            except OSError as e:
                out.append(f"[디스크] {target}: 조회 실패 ({e})")

    return _cap("\n".join(out) if out else "[요청한 섹션이 없음]", max_output)


_SYS_SECTIONS = ("all", "host", "cpu", "mem", "gpu", "models", "disk")


def _boot_elapsed() -> str:
    try:
        with open("/proc/uptime", encoding="ascii") as f:
            up = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return ""
    d, rem = divmod(int(up), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}일 {h}시간 {rem // 60}분"


def _nvidia_live(timeout: int) -> str:
    """GPU 별 실시간 VRAM/利用率. 없는 드라이버는 조용히 넘어간다."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        r = subprocess.run(
            [exe, "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    parts = []
    for line in r.stdout.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) != 5:
            continue
        parts.append(f"  GPU{f[0]}: {f[1]}  VRAM {f[3]}/{f[2]} MiB  부하 {f[4]}%")
    return "\n".join(parts)


def _live_models(timeout: int) -> str:
    """지금 GPU 에 상주해 있는 LLM. '설치된 모델'과 '실행 중인 모델'은 다르다."""
    exe = shutil.which("ollama")
    if not exe:
        return "ollama 없음 (설치/실행 중인 모델 조회 불가)"
    try:
        r = subprocess.run([exe, "ps"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "ollama ps 조회 실패"
    body = (r.stdout or "").strip()
    if r.returncode != 0 or not body:
        return "ollama 응답 없음"
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return "지금 상주 중인 모델 없음 (모든 모델 언로드됨)"
    return "\n".join("  " + ln for ln in lines)


TOOLS: Dict[str, ToolFn] = {
    "bash": _bash,
    "system_info": _system_info,
    "service_status": _service_status,
    "process_list": _process_list,
    "service_logs": _service_logs,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "find_files": _find_files,
    "disk_usage": _disk_usage,
    "grep_files": _grep_files,
    "excel_summary": _excel_summary,
    "excel_write": _excel_write,
    "pdf_read": _pdf_read,
    "image_ocr": _image_ocr,
    "remember": _remember,
    "recall": _recall,
    "forget": _forget,
    "memory_reflect": _memory_reflect,
    "memory_save_summary": _memory_save_summary,
    "schedule_add": _schedule_add,
    "schedule_list": _schedule_list,
    "schedule_remove": _schedule_remove,
    "study_save": _study_save,
    "study_recall": _study_recall,
    "geo_geocode": _geo_geocode,
    "geo_nearby": _geo_nearby,
    "geo_route": _geo_route,
    **webtools.TOOLS,
}

SCHEMAS = {
    "bash": "args: {command:str} — 샌드박스 셸 실행(차단패턴/타임아웃 적용)",
    "read_file": "args: {path:str, max_lines?:int} — 번호 매긴 파일 읽기",
    "write_file": "args: {path:str, content:str} — 파일 생성/전체 저장",
    "edit_file": "args: {path:str, old_string:str, new_string:str, replace_all?:bool, whole_word?:bool} — 부분 치환 (whole_word=true면 단어 단위만)",
    "list_dir": "args: {path?:str} — 디렉터리 목록",
    "grep_files": "args: {pattern:str, path?:str, max_matches?:int} — 정규식 내용 검색",
    "system_info": "args: {section?:'all'|'host'|'cpu'|'mem'|'gpu'|'models'|'disk'} — 지금 실행 중인 기기의 라이브 상태(CPU/RAM/GPU VRAM/상주 모델/디스크). 설치 목록이 아니라 구동 중 값",
    "service_status": "args: {state?:'all'|'active'|'failed'|'inactive'} — 서버의 사용자 서비스 상태 조회 (읽기 전용)",
    "process_list": "args: {limit?:int} — 서버에서 현재 실행 중인 프로세스 상위 CPU 목록 (읽기 전용)",
    "service_logs": "args: {service:str, lines?:int} — 사용자 서비스 최근 로그; 토큰/API 키 패턴은 마스킹",
    "find_files": "args: {path?:str, pattern:str, max_depth?:int, max_matches?:int} — 작업공간 안 이름 검색 (shell 미사용)",
    "disk_usage": "args: {path?:str} — 경로의 파일시스템 여유 공간과 제한된 상위 폴더 사용량",
    "excel_summary": "args: {path:str, sheet?:str, max_rows?:int} — .xlsx 열제목/행/숫자합계 요약",
    "excel_write": "args: {path:str, content:str} — Markdown 표를 .xlsx 시트로 저장",
    "pdf_read": "args: {path:str} — PDF를 텍스트로 추출(pdftotext)",
    "image_ocr": "args: {path:str, lang?:str(kor+eng)} — 사진/스캔 이미지를 OCR로 텍스트화",
    "remember": "args: {fact:str, max_len?:int} — 서버 파악 사실뿐 아니라 '엄마 생일 3월 3일' 같은 개인 사실도 기억에 저장 (폰별 분리, 재방문 방지)",
    "recall": "args: {} — 지금까지 기억한 사실 목록 조회 (이 사람 전용)",
    "schedule_add": "args: {title:str, start:str, end?:str, note?:str} — 일정 저장. start/end 는 서울시간(KST, UTC+9) ISO8601 (예: 2026-10-10T18:00:00). 빈 자리는 첫 표시 시각으로 잡는다.",
    "schedule_list": "args: {start?:str, end?:str} — 일정 조회. 범위 없으면 오늘 하루 (내일은 '2026-10-11T00:00' 처럼 start 지정)",
    "schedule_remove": "args: {key:str} — schedule_list 결과의 key 로 일정 삭제",
    "study_save": "args: {topic:str, content:str} — '미리 공부해놔' 요청용. web_search/web_fetch 로 조사한 핵심 정리를 주제별로 저장(폰별). 같은 주제는 대체된다.",
    "study_recall": "args: {topic?:str} — 저장된 학습 자료를 꺼낸다. topic 없으면 전부, 있으면 그 주제만.",
    "geo_geocode": "args: {query:str} — 주소/장소명 → 위경도+주소 (예: '서울역', '광화문 세종로 1')",
    "geo_nearby": "args: {kind:str, lat?:float, lon?:float, place?:str, radius?:m, limit?:n} — 반경 내 장소 목록. kind: 맛집/카페/관공서/병원/약국/은행/주유소/편의점/주차/역/공원. lat/lon 이 없으면 place 로 찾는다.",
    "geo_route": "args: {from:str, to:str} — 장소명 또는 '위도,경도' 두 지점 자동차 경로(거리·시간·주요구간)",
    "forget": "args: {} — 기억 전체 삭제",
    "memory_reflect": "args: {} — 지금 기억의 크기/본문을 보고 넘치면 요약으로 재정리할 수 있게 돌려준다",
    "memory_save_summary": "args: {summary:str} — 기억 전체를 3~5문장으로 압축해 저장(개별 사실은 최신 3개만 유지)",
    **webtools.SCHEMAS,
}


def _mcp_specs() -> dict:
    """연결된 MCP 도구 스펙. AUTORCODE_MCP_DISABLE=1 이면 건너뛴다."""
    if os.getenv("AUTORCODE_MCP_DISABLE"):
        return {}
    try:
        from . import mcp as mcp_mod
        return mcp_mod.tool_specs()
    except Exception:
        log.warning("MCP 도구 목록을 불러오지 못했습니다", exc_info=True)
        return {}


def schema_text() -> str:
    lines = [f"- {name}: {desc}" for name, desc in SCHEMAS.items()]
    for full, s in _mcp_specs().items():
        lines.append(f"- {full}: [MCP {s['server']}] {s['description']}")
    return "\n".join(lines)


_ARGS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\?)?:(.+)$")
_TYPE_MAP = {"str": "string", "int": "integer", "float": "number",
             "bool": "boolean", "object": "object", "list": "array"}


def _value_schema(val: str) -> dict:
    val = val.strip()
    m = re.match(r"^([A-Za-z]+)(?:\(([^)]*)\))?$", val)
    if m:
        base, comment = m.group(1), (m.group(2) or "").strip()
        schema = {"type": _TYPE_MAP.get(base, "string")}
        if comment:
            schema["description"] = comment
        return schema
    if "|" in val:
        enum = [t.strip().strip("'\"") for t in val.split("|")]
        return {"enum": enum, "type": "string"}
    return {"type": "string"}


def _json_schema(argspec: str) -> dict:
    """'args: {command:str, max_lines?:int}' 형식을 JSON Schema 로 변환한다."""
    start = argspec.find("{", argspec.find("args:"))
    if start < 0:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    depth = 0
    end = start
    for i in range(start, len(argspec)):
        if argspec[i] == "{":
            depth += 1
        elif argspec[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    inner = argspec[start + 1:end] if depth == 0 else ""
    properties, required = {}, []
    for token in inner.split(","):
        token = token.strip()
        if not token:
            continue
        m = _ARGS_RE.match(token)
        if not m:
            continue
        name, optional, val = m.group(1), bool(m.group(2)), m.group(3)
        properties[name] = _value_schema(val)
        if not optional:
            required.append(name)
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": True}


def native_schemas() -> list:
    """OpenAI function calling 용 스키마 목록. SCHEMAS 기술문이 단일 진실원이다."""
    out = []
    for name, desc in SCHEMAS.items():
        argspec = desc.split("—", 1)[0]
        out.append({"type": "function",
                    "function": {"name": name, "description": desc,
                                 "parameters": _json_schema(argspec)}})
    for full, s in _mcp_specs().items():
        out.append({"type": "function",
                    "function": {"name": full,
                                 "description": f"[MCP {s['server']}] {s['description']}",
                                 "parameters": s.get("inputSchema")
                                 or {"type": "object", "properties": {}}}})
    out.append({"type": "function",
                "function": {"name": "done",
                             "description": "모든 작업이 끝나 최종 답변을 낼 준비가 되면 호출하라. "
                                            "answer에 사용자에게 보일 최종/요약 답변을 채워라.",
                             "parameters": {"type": "object",
                                            "properties": {
                                                "thought": {"type": "string",
                                                            "description": "완료 근거 요약"},
                                                "answer": {"type": "string",
                                                           "description": "최종 답변"},
                                            },
                                            "required": ["answer"],
                                            "additionalProperties": True}}})
    return out


def execute(name: str, args: Args, root: str, max_output: int, timeout: int) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        try:
            from . import mcp as mcp_mod
            fn = mcp_mod.mcp_tool_fns().get(name)
        except Exception:
            fn = None
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
