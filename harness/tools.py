"""도구 레지스트리 + 실행기. 모든 출력은 상한으로 잘리고, 오류는 예외 대신 문자열로 환류."""
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict

from . import notes, safety, webtools

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


TOOLS: Dict[str, ToolFn] = {
    "bash": _bash,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "grep_files": _grep_files,
    "excel_summary": _excel_summary,
    "excel_write": _excel_write,
    "pdf_read": _pdf_read,
    "image_ocr": _image_ocr,
    "remember": _remember,
    "recall": _recall,
    "forget": _forget,
    **webtools.TOOLS,
}

SCHEMAS = {
    "bash": "args: {command:str} — 샌드박스 셸 실행(차단패턴/타임아웃 적용)",
    "read_file": "args: {path:str, max_lines?:int} — 번호 매긴 파일 읽기",
    "write_file": "args: {path:str, content:str} — 파일 생성/전체 저장",
    "edit_file": "args: {path:str, old_string:str, new_string:str, replace_all?:bool, whole_word?:bool} — 부분 치환 (whole_word=true면 단어 단위만)",
    "list_dir": "args: {path?:str} — 디렉터리 목록",
    "grep_files": "args: {pattern:str, path?:str, max_matches?:int} — 정규식 내용 검색",
    "excel_summary": "args: {path:str, sheet?:str, max_rows?:int} — .xlsx 열제목/행/숫자합계 요약",
    "excel_write": "args: {path:str, content:str} — Markdown 표를 .xlsx 시트로 저장",
    "pdf_read": "args: {path:str} — PDF를 텍스트로 추출(pdftotext)",
    "image_ocr": "args: {path:str, lang?:str(kor+eng)} — 사진/스캔 이미지를 OCR로 텍스트화",
    "remember": "args: {fact:str, max_len?:int} — 서버/시스템에서 파악한 사실을 오래 기억에 저장 (재방문 방지)",
    "recall": "args: {} — 지금까지 기억한 사실 목록 조회",
    "forget": "args: {} — 기억 전체 삭제",
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
