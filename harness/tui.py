"""autorcode TUI — opencode 스타일 전체화면 인터페이스 (Textual).

기존 하네스(harness.agent_core.Agent)를 그대로 재사용한다. 에이전트는 워커
스레드에서 돌고, 진행 이벤트(progress)는 ``TuiProgress`` 가 UI 로 중계한다.
도구 실행은 카드로, 파일 편집은 diff 로, 승인은 모달로 표현한다.

Textual 은 선택 의존성이다(``pip install 'autorcode[tui]'``). 없으면 이 모듈을
import 하는 시점이 아니라 ``run_tui`` 호출 시점에 안내 메시지와 함께 종료한다.

키:
   Enter      전송            Ctrl+Q   종료          Ctrl+P   커맨드 팔레트
   F2         모델 선택기      F3       세션 브라우저  Ctrl+T   테마 전환
   Ctrl+L     화면 지우기      Esc      현재 작업 취소
   Ctrl+C     선택 복사(없으면 종료)  Ctrl+Shift+C 선택 복사
   Ctrl+G     그림 첨부 — 클립보드 그림/파일을 창에 미리보기(▀)+OCR 로 뿌리고 다음 메시지에 전송
   Ctrl+V     텍스트 붙여넣기(여러 줄은 한 줄로 합침)
   /voice     음성방 참여 — 폰/PC 브라우저 마이크와 같은 대화(터미널은 자막·타이핑으로 참여)
   /duet      봇끼리 대화 — /duet <주제> 로 로이와 봇이 자동 대화(각자 다른 목소리), /duet stop 중단
   슬래시     /help /model /sessions /theme /status /tools /clear /auto /approve /pause /resume /cancel /compact /copy /image /voice /duet /quiet /quit
"""
import difflib
import glob
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from contextlib import contextmanager

from harness.progress import Activity, clean, result_status, short


# ------------------------------------------------------------------ API 확인
def _require_textual():
    try:
        import textual  # noqa: F401
    except ImportError:
        raise SystemExit(
            "[오류] TUI 모드는 Textual 이 필요합니다.\n"
            "  설치: pip install 'autorcode[tui]'   또는   pip install textual\n"
            "  (설치 후 다시: autorcode tui)"
        )


_TOOL_ICON = {
    "bash": "$", "read_file": "■", "write_file": "✎", "edit_file": "✎",
    "list_dir": "▤", "grep_files": "⌕", "find_files": "⌕",
    "web_search": "⌕", "web_fetch": "▼", "youtube": "▶",
    "remember": "✚", "recall": "↺", "forget": "␡",
}
_STATUS_STYLE = {"완료": "green", "실패": "red", "시간초과": "yellow", "거부": "yellow"}


# ------------------------------------------------------------------ 그림 첨부
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_image_name(name: str) -> bool:
    return str(name or "").lower().split("?")[0].endswith(_IMG_EXT)


def _img_ext_of(data: bytes) -> str:
    if data[:8] == _PNG_MAGIC:
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ""


def _run_clip_cmd(cmd, text=False, timeout=5):
    """클립보드 도구 한 번 실행 — 도구가 없거나 실패면 None."""
    if not shutil.which(cmd[0]):
        return None
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=text, timeout=timeout)
        return r if r.returncode == 0 else None
    except Exception:
        return None


def clipboard_text() -> str:
    """클립보드의 텍스트 — wl-paste → xclip → xsel → pbpaste."""
    for cmd in (["wl-paste", "--no-newline"],
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["pbpaste"]):
        r = _run_clip_cmd(cmd, text=True)
        if r is not None and (r.stdout or "").strip():
            return r.stdout
    return ""


def clipboard_image(dest_dir: str):
    """클립보드의 그림을 dest_dir 에 저장 → (경로, ""). 없거나 도구가 없으면 (None, 이유)."""
    binary_tools = [["wl-paste", "-t", "image/png"],
                    ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                    ["pngpaste", "-"]]
    have = [c for c in binary_tools if shutil.which(c[0])]
    if not have and not shutil.which("wl-paste") and not shutil.which("xclip"):
        return None, "클립보드 도구 없음(wl-paste/xclip/pngpaste 미설치)"
    for cmd in have:
        r = _run_clip_cmd(cmd)
        data = (r.stdout if r else b"") or b""
        ext = _img_ext_of(data)
        if ext:
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, f"tui_img_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
            with open(path, "wb") as fh:
                fh.write(data)
            return path, ""
    # 그림 파일을 복사한 경우: 파일 URI/경로 텍스트로 온다
    for cmd in (["wl-paste", "-t", "text/uri-list"],
                ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-o"],
                ["wl-paste", "--no-newline"],
                ["xclip", "-selection", "clipboard", "-o"]):
        r = _run_clip_cmd(cmd, text=True)
        text = (r.stdout if r else "").strip()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("file://"):
                line = urllib.parse.unquote(urllib.parse.urlparse(line).path)
            line = os.path.expanduser(line)
            if _is_image_name(line) and os.path.isfile(line):
                return os.path.realpath(line), ""
    return None, "클립보드에 그림이 없음"


def image_block_text(path: str, width: int = 48, height: int = 16):
    """그림을 터미널 반블록(▀, 24비트 색) 미리보기로 — PIL 없으면 None.

    각 글자 셀 = 세로 2픽셀(위=전경, 아래=배경) 이라 width x (height*2) 픽셀을
    가운데 맞춤으로 잘라 그린다. 어떤 터미널에서든 그대로 보인다(트루컬러)."""
    try:
        from PIL import Image, ImageOps
        from rich.text import Text
    except ImportError:
        return None
    try:
        im = Image.open(path)
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        return None
    width = max(8, int(width))
    height = max(2, int(height))
    im = ImageOps.fit(im, (width, height * 2), method=Image.LANCZOS)
    px = im.load()
    out = Text()
    for y in range(height):
        for x in range(width):
            r1, g1, b1 = px[x, y * 2]
            r2, g2, b2 = px[x, y * 2 + 1]
            out.append("▀", style=f"#{r1:02x}{g1:02x}{b1:02x} on #{r2:02x}{g2:02x}{b2:02x}")
        out.append("\n")
    return out


def ocr_text(path: str, lang: str = "kor+eng", timeout: int = 60) -> str:
    """그림 속 글자를 tesseract 로 읽는다 — 없거나 실패면 빈 문자열."""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    import subprocess
    try:
        r = subprocess.run([tesseract, path, "stdout", "-l", lang],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _diff_text(old: str, new: str, path: str):
    """unified diff 를 Rich Text(색상)로 만든다. 내용이 같으면 None."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines,
                                     fromfile=path or "이전", tofile=path or "이후", n=2))
    if not diff:
        return None
    from rich.text import Text
    out = Text()
    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            out.append(line + "\n", style="bold dim")
        elif line.startswith("@@"):
            out.append(line + "\n", style="cyan")
        elif line.startswith("+"):
            out.append(line + "\n", style="green")
        elif line.startswith("-"):
            out.append(line + "\n", style="red")
        else:
            out.append(line + "\n", style="dim")
    return out


def _clip(text, limit=400):
    """한 줄을 폭 제한까지만 남긴다. 공백은 보존한다(출력 가독성)."""
    text = clean(text).rstrip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ------------------------------------------------------------------ 진행 브리지
class TuiProgress:
    """agent_core 가 stderr 대신 이 객체로 진행 상황을 흘려보낸다.

    모든 메서드는 워커 스레드에서 호출되므로 UI 변경은 call_from_thread 로
    앱 스레드에 위임한다. agent_core 가 기대하는 속성(enabled/details)과
    메서드(event/activity/update/result/tool_start)를 모두 제공한다.
    """

    def __init__(self, app):
        self.app = app
        self.enabled = True
        self.details = True

    def event(self, text):
        self.app.call_from_thread(self.app.handle_event, str(text))

    @contextmanager
    def activity(self, label):
        act = Activity(label)
        self.app.call_from_thread(self.app.handle_activity_start, str(label), act)
        try:
            yield act
        finally:
            self.app.call_from_thread(self.app.handle_activity_end)

    def update(self, activity, message):
        self.app.call_from_thread(self.app.handle_activity_update, short(message))

    def result(self, label, result, elapsed):
        self.app.call_from_thread(self.app.handle_result, str(label), str(result), float(elapsed))

    def tool_start(self, name, args, label):
        self.app.call_from_thread(self.app.handle_tool_start, str(name), dict(args), str(label))

    def thought(self, step, text):
        self.app.call_from_thread(self.app.handle_thought, int(step), short(text))

    def text_delta(self, text):
        self.app.call_from_thread(self.app.handle_text_delta, str(text))

    def text_reset(self):
        self.app.call_from_thread(self.app.handle_text_reset)


def build(cfg, model="", permissions=None):
    """cli.cmd_tui 진입점 — Textual 앱을 만들어 실행한다."""
    return run(cfg, model=model, permissions=permissions)


# ================================================================== 앱
def _build_app_classes():
    """Textual 을 import 한 뒤에만 위젯/스크린 클래스를 정의한다."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button, Input, Markdown, OptionList, Static,
    )
    from textual.widgets.option_list import Option
    from rich.text import Text

    # ---------------- 위젯 ----------------
    class PromptInput(Input):
        """한 줄 입력. 여러 줄 붙여넣기는 공백으로 합쳐 받는다.

        Textual 기본 Input 은 붙여넣기에서 첫 줄만 넣어, 여러 줄 지시문을
        복사하면 나머지가 잘린다. 여기서는 모든 줄을 이어 붙인다.
        """

        def _on_paste(self, event) -> None:
            # Textual 은 MRO 를 따라 Input._on_paste(첫 줄만 넣음)까지 함께
            # 호출한다. prevent_default() 로 기본 동작을 끊어야 중복되지 않는다.
            event.prevent_default()
            text = str(getattr(event, "text", "") or "")
            flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
            if flat:
                self.insert_text_at_cursor(flat)
            event.stop()

    class Notice(Static):
        def __init__(self, text, kind="info"):
            super().__init__(Text(clean(text), style="dim"), classes=f"notice {kind}")

    class UserMessage(Static):
        def __init__(self, text):
            super().__init__(Text("› " + text, style="bold"), classes="user")

    class AssistantMessage(Markdown):
        def __init__(self, text):
            super().__init__(text, classes="assistant")

    class ImagePreview(Static):
        """첨부한 그림 — 터미널 컬러 반블록 미리보기(▀). PIL/그림 문제면 한 줄 안내."""

        def __init__(self, path, width=48, height=16):
            text = image_block_text(path, width, height)
            if text is None:
                super().__init__(Text(f"(미리보기 없음) {path}", style="dim"), classes="imgprev")
            else:
                super().__init__(text, classes="imgprev")
            try:
                size = os.path.getsize(path)
                self.border_title = f" {os.path.basename(path)} ({size:,}B) "
            except OSError:
                self.border_title = f" {path} "
            self.border_subtitle = " 첨부됨 "
            self.path = path

    class OcrText(Static):
        """그림 속 글자(OCR) — tesseract 결과를 그대로 뿌린다."""

        def __init__(self, path, text):
            body = Text()
            body.append(f"그림 속 글자 — {os.path.basename(path)}\n", style="bold")
            body.append(clean(text) or "(글자를 찾지 못했습니다)")
            super().__init__(body, classes="ocrtext")
            self.border_title = " OCR "
            self.border_subtitle = f" {len(text.splitlines())}줄 "

    class StepLine(Static):
        """단계 헤더 — 모델이 이번 단계에서 밝힌 계획(thought)을 한 줄로."""

        def __init__(self, step, thought):
            super().__init__(Text.assemble(
                (f"\n◆ {step}단계 ", "bold cyan"),
                (short(thought, 240), ""),
            ), classes="step")

    class ToolCard(Vertical):
        """도구 실행 카드 — opencode 스타일 테두리 박스.

        테두리 제목에 아이콘+도구명, 우측에 상태(완료/실패·시간·크기)를 두고,
        본문에 명령 라벨과 결과 출력을 펼쳐서 그린다. 파일 쓰기/편집은 diff 로.

        결과는 상태로만 저장하고 compose 에서 그린다. 도구가 빨리 끝나
        mount 직후 result 가 오면 자식이 아직 안 만들어졌을 수 있으므로
        query_one 대신 refresh(recompose=True) 로 다시 그린다.
        """

        BORDER_TITLE_ALIGN = "left"
        BORDER_SUBTITLE_ALIGN = "right"

        def __init__(self, name, args, label):
            super().__init__(classes="tool")
            self.tool_name = name
            self.tool_args = args or {}
            self.tool_label = label
            self._result = None
            self._elapsed = 0.0
            self._old_content = self._read_old(name, self.tool_args)
            self.border_title = self._title()
            self.border_subtitle = self._subtitle()

        @staticmethod
        def _read_old(name, args):
            if name != "write_file":
                return None
            path = os.path.join(_workspace_root[0], str(args.get("path", "")))
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        return fh.read()
                return ""
            except OSError:
                return None

        def _title(self):
            icon = _TOOL_ICON.get(self.tool_name, "•")
            name = self.tool_name or "tool"
            return f" {icon} {name} "

        def _subtitle(self):
            if self._result is None:
                return " 실행 중… "
            status = result_status(self._result)
            return f" {status} · {self._elapsed:.1f}s · {len(self._result):,}자 "

        def _head(self):
            if self._result is None:
                return Text.assemble(
                    (short(self.tool_label, 200), "bold"),
                    ("   실행 중…", "dim italic"),
                )
            status = result_status(self._result)
            style = _STATUS_STYLE.get(status, "green")
            return Text.assemble(
                (short(self.tool_label, 200), "bold"),
                (f"   {status}", style),
            )

        def _body_widgets(self):
            if self._result is None:
                return []
            diff = None
            if self.tool_name == "edit_file":
                diff = _diff_text(str(self.tool_args.get("old_string", "")),
                                  str(self.tool_args.get("new_string", "")),
                                  str(self.tool_args.get("path", "")))
            elif self.tool_name == "write_file":
                diff = _diff_text(self._old_content or "",
                                  str(self.tool_args.get("content", "")),
                                  str(self.tool_args.get("path", "")))
            if diff is not None:
                return [Static(diff, classes="tool-body")]

            lines = [ln for ln in clean(self._result).splitlines() if ln.strip()]
            status = result_status(self._result)
            limit = 30 if status == "완료" else 45
            body = "\n".join(_clip(ln, 500) for ln in lines[:limit])
            if len(lines) > limit:
                body += f"\n… {len(lines) - limit}줄 더"
            style = "" if status == "완료" else "red"
            return [Static(Text(body or _clip(self._result, 300), style=style),
                           classes="tool-body")]

        def compose(self) -> ComposeResult:
            yield Static(self._head(), classes="tool-head")
            yield from self._body_widgets()

        def set_result(self, result, elapsed):
            self._result = result
            self._elapsed = elapsed
            self.border_subtitle = self._subtitle()
            self.remove_class("ok", "warn", "fail")
            status = result_status(result)
            if status == "완료":
                self.add_class("ok")
            elif status in ("거부", "시간초과"):
                self.add_class("warn")
            else:
                self.add_class("fail")
            try:
                self.refresh(recompose=True)
            except Exception:
                pass

    # ---------------- 모달 ----------------
    class ApproveScreen(ModalScreen):
        BINDINGS = [Binding("y", "yes", "승인", priority=True),
                    Binding("n", "no", "거부", priority=True),
                    Binding("escape", "no", "거부", priority=True)]

        def __init__(self, why):
            super().__init__()
            self.why = why

        def compose(self) -> ComposeResult:
            with Vertical(classes="dialog"):
                yield Static("⚠  승인이 필요합니다", classes="dialog-title")
                yield Static(Text(clean(self.why), style=""), classes="dialog-body")
                with Vertical(classes="dialog-buttons"):
                    yield Button("승인 (y)", variant="success", id="ok")
                    yield Button("거부 (n)", variant="error", id="no")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "ok")

        def action_yes(self):
            self.dismiss(True)

        def action_no(self):
            self.dismiss(False)

    class ModelPicker(ModalScreen):
        """모델 선택기 — 카탈로그 엔트리(이름+백엔드+출처)를 그대로 돌려준다.

        이름만 돌려주면 ollama 모델과 원격 서버 모델이 뒤섞여 있을 때
        백엔드를 다시 추론해야 하는데, 같은 이름이 두 서버에 있으면 뒤바뀐다.
        """
        BINDINGS = [Binding("escape", "cancel", "취소")]

        def __init__(self, entries, current):
            super().__init__()
            self.entries = entries
            self.current = current

        def compose(self) -> ComposeResult:
            with Vertical(classes="dialog"):
                yield Static("모델 선택 (Enter) · Esc 취소", classes="dialog-title")
                opts = []
                for i, e in enumerate(self.entries):
                    mark = "● " if e["name"] == self.current else "  "
                    label = Text.assemble(
                        (mark + e["name"], ""),
                        (f"   {e['source']}", "dim"),
                    )
                    opts.append(Option(label, id=str(i)))
                yield OptionList(*opts, id="models")

        def on_mount(self):
            box = self.query_one("#models", OptionList)
            idx = 0
            try:
                idx = next(i for i, e in enumerate(self.entries)
                           if e["name"] == self.current)
            except StopIteration:
                pass
            box.highlighted = idx  # 기본값은 None — Enter 가 아무것도 고르지 않음
            box.focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected):
            try:
                self.dismiss(self.entries[int(event.option.id)])
            except (TypeError, ValueError, IndexError):
                self.dismiss(None)

        def action_cancel(self):
            self.dismiss(None)

    class SessionPicker(ModalScreen):
        BINDINGS = [Binding("escape", "cancel", "취소")]

        def __init__(self, files, current):
            super().__init__()
            self.files = files
            self.current = current

        def compose(self) -> ComposeResult:
            with Vertical(classes="dialog"):
                yield Static("세션 선택 (Enter) · 새 대화는 Esc", classes="dialog-title")
                if not self.files:
                    yield Static(Text("  저장된 세션이 없습니다", style="dim"))
                opts = []
                for path in self.files:
                    try:
                        st = os.stat(path)
                        meta = f"{st.st_size//1024}KB · {time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))}"
                        n = sum(1 for _ in open(path, encoding="utf-8", errors="replace"))
                    except OSError:
                        meta, n = "", 0
                    mark = "● " if path == self.current else "  "
                    opts.append(Option(f"{mark}{os.path.basename(path)}  ({meta} · {n}줄)",
                                       id=path))
                if opts:
                    yield OptionList(*opts, id="sessions")

        def on_mount(self):
            if self.files:
                self.query_one("#sessions", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected):
            self.dismiss(event.option.id)

        def action_cancel(self):
            self.dismiss(None)

    # ---------------- 메인 앱 ----------------
    class AutorcodeTUI(App):
        CSS = """
        Screen { layout: vertical; }
        #status { dock: top; height: 1; background: $panel; color: $text; padding: 0 1; }
        #log { height: 1fr; padding: 0 1; }
        #activity { height: 1; color: $text-muted; padding: 0 2; }
        #slashmenu { height: auto; max-height: 8; display: none; margin: 0 1;
                     border: round $accent 45%; background: $surface; }
        #slashmenu > .option-list--option { padding: 0 1; }
        #slashmenu > .option-list--option-highlighted { background: $accent 25%; }
        #prompt { dock: bottom; }
        .user { color: $text; margin: 1 0 0 0; }
        .assistant { margin: 0 0 1 0; }
        .notice { color: $text-muted; }
        .notice.error { color: $error; }
        .notice.warn { color: $warning; }
        .notice.ok { color: $success; }
        .step { color: $text; height: auto; }
        .tool { margin: 0 0 0 0; padding: 0 1; height: auto;
                border: round $accent 45%; background: $surface; }
        .tool.ok { border: round $success 45%; }
        .tool.warn { border: round $warning 55%; }
        .tool.fail { border: round $error 65%; }
        .tool-head { color: $text; padding: 0; height: auto; }
        .tool-body { color: $text-muted; padding: 0 0 0 1; height: auto; }
        .dialog { width: 80%; max-width: 100; height: auto; max-height: 80%;
                  border: thick $accent; background: $surface; padding: 1 2; }
        .dialog-title { text-style: bold; padding: 0 0 1 0; }
        .dialog-body { padding: 0 0 1 0; }
        .dialog-buttons { height: auto; }
        .imgprev { width: auto; height: auto; border: round $accent 55%; background: $surface;
                   padding: 0 1; margin: 0 0 0 0; }
        .ocrtext { width: auto; height: auto; border: round $success 45%; background: $surface;
                   padding: 0 1; margin: 0 0 0 0; color: $text; }
        """
        ALLOW_SELECT = True
        BINDINGS = [
            Binding("ctrl+q", "quit_app", "종료"),
            Binding("ctrl+c", "copy_or_quit", "복사/종료", priority=True),
            Binding("ctrl+shift+c", "copy_selection", "선택 복사"),
            Binding("ctrl+v", "paste_text", "붙여넣기", priority=True),
            Binding("ctrl+g", "paste_image", "그림 첨부", priority=True),
            Binding("f2", "model_picker", "모델"),
            Binding("f3", "session_picker", "세션"),
            Binding("ctrl+t", "cycle_theme", "테마"),
            Binding("ctrl+l", "clear_log", "지우기"),
            Binding("escape", "cancel_run", "취소"),
        ]

        def __init__(self, cfg, model="", permissions=None):
            super().__init__()
            self.cfg = cfg
            self.initial_model = model
            self.permissions = permissions or cfg.permissions_mode
            self.agent = None
            self.busy = False
            self._activity = None
            self._recv = ""
            self._cards = {}
            self._spin = 0
            self._f2_first = True
            self._project_approved = bool(cfg.auto_yes)
            self._paused = False
            self._attachment = []
            self._live = None
            self._live_buf = ""
            self._live_json = False
            self._live_at = 0.0
            self._voice = None
            self._voice_on = False
            self._voice_live = None
            self._voice_buf = ""
            self._voice_speaker = None

        # ---- 수명주기 ----
        def compose(self) -> ComposeResult:
            yield Static(id="status")
            with VerticalScroll(id="log"):
                yield Static(id="welcome")
            yield Static(id="activity")
            yield OptionList(id="slashmenu")
            yield PromptInput(placeholder="메시지 입력  (/help · F2 모델 · F3 세션 · Ctrl+T 테마)",
                              id="prompt")

        def on_mount(self):
            self.title = "autorcode"
            self._workspace_ref()
            self._build_agent()
            log = self.query_one("#log", VerticalScroll)
            log.query_one("#welcome").update(Text.assemble(
                ("  autorcode TUI\n", "bold"),
                (f"  모델 {self.cfg.model_fast}  ·  작업 위치 {self.cfg.workspace_root}\n", "dim"),
                ("  Enter 전송 · Ctrl+V 붙여넣기 · Ctrl+G 그림 · Ctrl+P 팔레트 · F2 모델 · F3 세션 · Ctrl+T 테마\n", "dim"),
            ))
            self.query_one("#prompt", Input).focus()
            self.query_one("#slashmenu", OptionList).display = False
            self._refresh_status()
            self.set_interval(0.12, self._tick)

        def _workspace_ref(self):
            _workspace_root[0] = self.cfg.workspace_root

        # ---- 에이전트 ----
        def _build_agent(self):
            from harness import agent_core
            if self.agent is not None:
                try:
                    self.agent.close()
                except Exception:
                    pass
            self.agent = agent_core.Agent(
                self.cfg,
                confirmer=self._confirmer,
                progress=TuiProgress(self),
            )
            if self._paused:
                self.agent.pause()
            else:
                self.agent.resume()

        def run(self, *a, **kw):
            _workspace_root[0] = self.cfg.workspace_root
            return super().run(*a, **kw)

        # ---- 승인 ----
        def _confirmer(self, why):
            if self._project_approved or self.cfg.auto_yes:
                return True
            ev = threading.Event()
            box = {}
            self.call_from_thread(self._push_approve, why, ev, box)
            ev.wait()
            return bool(box.get("ok", False))

        def _push_approve(self, why, ev, box):
            def done(ok):
                ok = bool(ok)
                box["ok"] = ok
                if ok:
                    self._approve_project()
                ev.set()
            try:
                self.push_screen(ApproveScreen(why), done)
            except Exception:
                box["ok"] = False
                ev.set()

        def _approve_project(self):
            if not self._project_approved:
                self._project_approved = True
                self.cfg.auto_yes = True
                self._notice(f"프로젝트 전체 승인됨: {self.cfg.workspace_root}", "ok")
                self._refresh_status()

        # ---- 진행 이벤트 ----
        def handle_event(self, text):
            text = text.strip()
            if not text:
                return
            if text.startswith("[시작]"):
                body = text[len("[시작]"):].strip()
                parts = [p.strip() for p in body.split("·")]
                if parts:
                    self._status_model = parts[0]
                if len(parts) > 1:
                    self._status_tier = parts[1]
                if "작업 위치:" in body:
                    self._status_ws = body.split("작업 위치:")[-1].strip()
                self._refresh_status()
                return
            import re as _re
            m = _re.match(r"\[(\d+)/(\d+)\]", text)
            if m:
                self._status_step = (m.group(1), m.group(2))
                self._refresh_status()
                if any(k in text for k in ("재요청", "위반", "실패", "중단", "거부")):
                    self._notice(text, "warn")
                return
            if any(k in text for k in ("승인", "거부", "중단", "오류", "실패")):
                kind = "error" if ("오류" in text or "실패" in text) else "warn"
                self._notice(text, kind)
            elif text.startswith("[완료]"):
                self._notice(text, "ok")

        def handle_activity_start(self, label, act):
            self._activity = (label, act.started, time.monotonic())
            self._recv = ""
            self._live = None
            self._live_buf = ""
            self._live_json = False
            self._render_activity()

        def handle_activity_update(self, message):
            self._recv = message
            self._render_activity()

        def handle_activity_end(self):
            if self._live is not None:
                # 스로틀에 밀린 마지막 조각까지 반영하고 마무리한다
                try:
                    self._live.update(Text(clean(self._live_buf)))
                    self.query_one("#log", VerticalScroll).scroll_end(animate=False)
                except Exception:
                    pass
            self._activity = None
            self._recv = ""
            self._render_activity()

        def handle_text_delta(self, text):
            """수신 중인 모델 글자를 로그에 실시간으로 흘린다 (여기처럼 쫙쫙)."""
            if not text or self._live_json:
                return
            self._live_buf += text
            if self._live is None:
                head = self._live_buf.lstrip()
                if not head:
                    return
                if head[:1] in ("{", "["):
                    # JSON 계획 응답 — 원시 JSON 은 뿌리지 않는다 (완료 후 thought 로만)
                    self._live_json = True
                    return
                self._live = Static(Text(clean(self._live_buf)), classes="step")
                self._mount(self._live)
                self._live_at = time.monotonic()
                return
            now = time.monotonic()
            if now - self._live_at < 0.08:
                return  # 리렌더 스로틀 — 조각마다 그리면 오래 걸린다
            self._live_at = now
            try:
                self._live.update(Text(clean(self._live_buf)))
                self.query_one("#log", VerticalScroll).scroll_end(animate=False)
            except Exception:
                pass

        def handle_text_reset(self):
            """스트리밍 재시도 — 지금까지 흘린 조각은 버린다."""
            if self._live is not None:
                try:
                    self._live.remove()
                except Exception:
                    pass
            self._live = None
            self._live_buf = ""
            self._live_json = False

        def handle_thought(self, step, text):
            if not text:
                return
            if self._live is not None:
                # 이미 실시간으로 흘려보낸 내용 — 단계 배지로 확정만 한다(전문 유지)
                live, buf = self._live, self._live_buf.strip()
                self._live, self._live_buf = None, ""
                try:
                    live.update(Text.assemble(
                        (f"\n◆ {step}단계 ", "bold cyan"),
                        (clean(buf), ""),
                    ))
                except Exception:
                    pass
                return
            self._mount(StepLine(step, text))

        def handle_tool_start(self, name, args, label):
            card = ToolCard(name, args, label)
            self._cards.setdefault(label, []).append(card)
            self._mount(card)

        def handle_result(self, label, result, elapsed):
            stack = self._cards.get(label) or []
            if stack:
                card = stack.pop(0)
            else:
                card = ToolCard("", {}, label)
                self._mount(card)
            card.set_result(result, elapsed)

        def _render_activity(self):
            try:
                w = self.query_one("#activity", Static)
            except Exception:
                return
            if not self._activity:
                w.update("")
                return
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            ch = frames[self._spin % len(frames)]
            label = short(self._activity[0], 100)
            elapsed = time.monotonic() - self._activity[2]
            extra = f" · {self._recv}" if self._recv else ""
            w.update(Text(f"  {ch} {label} · {elapsed:.1f}s{extra}", style="dim"))

        def _tick(self):
            self._spin += 1
            if self._activity:
                self._render_activity()

        # ---- 로그 헬퍼 ----
        def _mount(self, widget):
            log = self.query_one("#log", VerticalScroll)
            log.mount(widget)
            log.scroll_end(animate=False)

        def _notice(self, text, kind="info"):
            self._mount(Notice(text, kind))

        def _refresh_status(self):
            try:
                w = self.query_one("#status", Static)
            except Exception:
                return
            model = getattr(self, "_status_model", self.cfg.model_fast)
            tier = getattr(self, "_status_tier", "-")
            step = getattr(self, "_status_step", None)
            step_s = f"step {step[0]}/{step[1]}" if step else ""
            ws = short(getattr(self, "_status_ws", self.cfg.workspace_root), 40)
            perm = self.cfg.permissions_mode + ("/auto" if self.cfg.auto_yes else "") \
                + ("/pause" if self._paused else "")
            right = f"{step_s} · 권한 {perm}".strip()
            parts = [
                (" autorcode ", "bold reverse"),
                (f"  {model} ", "bold"),
                (f"[{tier}] ", "dim"),
                (f"· {ws}", "dim"),
                (f"   {right}", "dim"),
            ]
            if getattr(self, "_attachment", None):
                parts.append((f"  첨부 {len(self._attachment)}", "bold yellow"))
            if self._voice_on:
                parts.append(("  🎤 음성방", "bold green"))
            w.update(Text.assemble(*parts))

        # ---- 입력 ----
        def on_input_changed(self, event: Input.Changed):
            self._update_slash_menu(event.value)

        def _update_slash_menu(self, val):
            """'/' 로 시작하면 매칭되는 슬래시 명령을 클릭 가능한 메뉴로 띄운다."""
            try:
                menu = self.query_one("#slashmenu", OptionList)
            except Exception:
                return
            if not val.startswith("/"):
                menu.display = False
                return
            matches = [c for c in SLASH_HELP if c.startswith(val)]
            if not matches:
                menu.display = False
                return
            menu.clear_options()
            for c in matches:
                menu.add_option(Option(f"{c}   {SLASH_HELP[c]}", id=c))
            try:
                menu.highlighted = 0
            except Exception:
                pass
            menu.display = True

        def _hide_slash_menu(self):
            try:
                self.query_one("#slashmenu", OptionList).display = False
            except Exception:
                pass

        def on_option_list_option_selected(self, event: OptionList.OptionSelected):
            """슬래시 메뉴에서 명령을 클릭/선택하면 바로 실행한다."""
            if getattr(event.option_list, "id", None) != "slashmenu":
                return
            cmd = event.option.id or ""
            self._hide_slash_menu()
            prompt = self.query_one("#prompt", Input)
            prompt.value = ""
            prompt.focus()
            if cmd:
                self._slash(cmd)

        def on_input_submitted(self, event: Input.Submitted):
            task = event.value.strip()
            self.query_one("#prompt", Input).value = ""
            self._hide_slash_menu()
            if not task:
                return
            if task.startswith("/"):
                self._slash(task)
                return
            if self._voice_on:
                if self._voice and self._voice.send(task):
                    return
                self.query_one("#prompt", Input).value = task
                self._notice("음성방 연결이 아직 없습니다 — 잠시 후 다시 Enter, 나가려면 /voice off", "warn")
                return
            full, atts = self._compose_task(task)
            shown = task + (f"   [첨부: {', '.join(os.path.basename(a) for a in atts)}]" if atts else "")
            if self.busy:
                if self._paused and self.agent:
                    self._mount(UserMessage(shown))
                    try:
                        self.agent.mem.add("user", full)
                        self._notice("일시정지 상태에서 메시지를 추가했습니다. /resume 로 계속하세요.", "ok")
                    except Exception as e:
                        self._notice(f"메시지 추가 실패: {e}", "error")
                    return
                self._notice("이미 작업이 실행 중입니다 — /pause 후 대화하거나 Esc로 취소하세요", "warn")
                return
            self._mount(UserMessage(shown))
            self.busy = True
            self._run_task(full)

        @work(thread=True, exclusive=True)
        def _run_task(self, task):
            try:
                answer = self.agent.run(task)
            except Exception as e:  # 워커 예외가 UI 를 죽이지 않게
                answer = f"[오류] {e}"
            self.call_from_thread(self._finish_task, answer)

        def _finish_task(self, answer):
            self.busy = False
            self.handle_activity_end()
            summary, ans = _split_answer(answer)
            if self._live is not None and self._live_buf.strip():
                # 이미 실시간으로 다 흘린 평문 답 — 그대로 두면 완료 메시지와 겹친다.
                # 평문 블록을 걷고 마크다운 하나로 다시 그린다.
                if " ".join(self._live_buf.split()) == " ".join(ans.split()):
                    try:
                        self._live.remove()
                    except Exception:
                        pass
                    self._live = None
                    self._live_buf = ""
            if summary:
                self._notice(summary, "ok")
            if ans:
                self._mount(AssistantMessage(ans))

        # ---- 슬래시 명령 ----
        def _slash(self, task):
            cmd = task.split()[0]
            rest = task[len(cmd):].strip()
            if cmd == "/help":
                self._notice("명령: " + " · ".join(f"{k} {v}" for k, v in SLASH_HELP.items()))
            elif cmd == "/model":
                self.action_model_picker()
            elif cmd in ("/sessions", "/session"):
                self.action_session_picker()
            elif cmd == "/theme":
                self.action_cycle_theme()
            elif cmd == "/clear":
                self.action_clear_log()
            elif cmd == "/status":
                self._notice(self._status_text())
            elif cmd == "/tools":
                from harness import tools as tools_mod
                self._notice(f"도구 {len(tools_mod.TOOLS)}개: " +
                             ", ".join(sorted(tools_mod.TOOLS)))
            elif cmd == "/auto":
                self.cfg.auto_smart = rest not in ("off", "0", "false")
                self._notice(f"자율 모드 {'on' if self.cfg.auto_smart else 'off'}")
                self._refresh_status()
            elif cmd == "/approve":
                on = rest not in ("off", "0", "false")
                self._project_approved = on
                self.cfg.auto_yes = on
                self._notice(f"프로젝트 전체 승인 {'on' if on else 'off'}")
                self._refresh_status()
            elif cmd == "/pause":
                self._pause_run()
            elif cmd == "/resume":
                self._resume_run()
            elif cmd == "/cancel":
                self.action_cancel_run()
            elif cmd == "/compact":
                if self.busy:
                    self._notice("실행 중에는 /compact 을 쓸 수 없습니다", "warn")
                elif self.agent:
                    summary = self.agent.compact_session()
                    self._notice(f"세션을 요약·정리했습니다 · 현재 약 {self.agent.mem.size()}tok")
                    if summary:
                        self._mount(AssistantMessage("[세션 요약]\n" + summary))
                else:
                    self._notice("아직 agent가 없습니다", "warn")
            elif cmd == "/copy":
                if rest == "all":
                    self.action_copy_last()
                elif rest == "image":
                    self.action_copy_image()
                elif rest.startswith("share"):
                    arg = rest[5:].strip()
                    if arg:
                        self._share_file(os.path.expanduser(arg))
                    else:
                        self._notice("사용법: /copy share <파일경로>", "warn")
                else:
                    self.action_copy_selection()
            elif cmd in ("/image", "/img"):
                if rest:
                    self._attach_path(rest)
                else:
                    self._attach_clipboard()
            elif cmd == "/voice":
                self._voice_toggle(rest)
            elif cmd == "/duet":
                self._duet_cmd(rest)
            elif cmd == "/quiet":
                self._notice("TUI 는 --quiet 대신 카드 접기로 대체됩니다")
            elif cmd in ("/quit", "/exit"):
                self.action_quit_app()
            else:
                self._notice(f"알 수 없는 명령: {cmd} — /help", "warn")

        def _status_text(self):
            c = self.cfg
            return (f"모델 {c.model_fast} · 작업위치 {c.workspace_root} · 권한 {c.permissions_mode} · "
                    f"세션 {c.session_file or '(없음)'} · 도구 {len(__import__('harness.tools', fromlist=['TOOLS']).TOOLS)}개")

        # ---- 액션 ----
        def action_quit_app(self):
            self.exit()

        def action_copy_or_quit(self):
            text = self._selected_text()
            if text:
                self._copy_text(text)
            else:
                self.action_quit_app()

        def action_copy_last(self):
            # /copy — 마지막 어시스턴스 메시지 전체 복사
            try:
                msgs = self.query(AssistantMessage)
                last = list(msgs)[-1]
                text = last.source if hasattr(last, "source") and last.source else last.renderable
                self._copy_text(str(text))
            except IndexError:
                self._notice("복사할 답변이 없습니다", "warn")
            except Exception as e:
                self._notice(f"복사 실패: {e}", "warn")

        def _share_file(self, path):
            # 플링파일(flileshare) share 폴더로 옮겨 폰/PC 링크로 열 수 있게 한다
            import shutil, time
            root = os.environ.get("AUTORCODE_SHARE_DIR",
                                  os.path.expanduser("~/flingfile/share"))
            if not os.path.isdir(root):
                self._notice("플링파일 공유 폴더가 없어 공유 불가 — 사본 파일로 옮기세요", "warn")
                return
            try:
                base = os.path.basename(path)
                stem, ext = os.path.splitext(base)
                dst = os.path.join(root, f"{stem}_{time.strftime('%m%d_%H%M')}{ext}")
                shutil.copyfile(path, dst)
                rel = dst[len(root) + 1:]
                try:
                    import urllib.parse as _up
                    import urllib.request as _ur
                    req = _ur.Request("http://127.0.0.1:8080/api/download?path=" + _up.quote(rel))
                    code = _ur.urlopen(req, timeout=5).status
                except Exception:
                    code = "?"
                self._notice(f"공유됨(HTTP {code}): 플링파일 목록 → {rel} "
                             "(qq.hostingtower.cloud:8080)", "ok")
            except Exception as e:
                self._notice(f"공유 실패: {e}", "warn")

        def action_copy_image(self):
            # /copy image — 마지막 첨부 그림을 사본으로 내보내기
            try:
                imgs = [w for w in self.query(ImagePreview)]
                path = imgs[-1].path if imgs and getattr(imgs[-1], "path", None) else None
            except Exception:
                path = None
            if not path or not os.path.isfile(path):
                self._notice("첨부된 그림이 없습니다 — /image <경로> 로 먼저 붙이세요", "warn")
                return
            try:
                import shutil, tempfile
                dst = os.path.join(tempfile.gettempdir(), "autorcode-copy" + os.path.splitext(path)[1])
                shutil.copyfile(path, dst)
                self._notice(f"그림 사본: {dst}", "info")
            except Exception as e:
                self._notice(f"그림 복사 실패: {e}", "warn")
            self._share_file(path)

        def action_copy_selection(self):
            text = self._selected_text()
            if not text:
                self._notice("복사할 선택이 없습니다 — 마우스로 드래그해 선택하세요", "warn")
                return
            self._copy_text(text)

        def _selected_text(self):
            try:
                return self.screen.get_selected_text()
            except Exception:
                return None

        def _osc52_copy(self, text):
            # 터미널 OSC52로 복사 (SSH/터미널이 지원해야 실제로 들어감)
            try:
                self.copy_to_clipboard(text)
                return True
            except Exception:
                return False

        def _copy_text(self, text):
            # 이 창은 SSH/폰 터미널로 접속되는 경우가 많아 OSC52가 차단되기 쉽다.
            # 그래서 복사 성공 여부와 무관하게 사본 파일을 항상 남긴다 —
            # 붙여넣기가 안 되면 그 파일로 옮기면 된다. 정곡: 결과물을 두 곳 남긴다.
            saved = None
            try:
                import os, tempfile
                saved = os.path.join(tempfile.gettempdir(), "autorcode-copy.txt")
                with open(saved, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                saved = None
            import shutil
            import subprocess
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                        ["xsel", "--clipboard", "--input"], ["pbcopy"]):
                if not shutil.which(cmd[0]):
                    continue
                try:
                    subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=2)
                    self._notice(f"클립보드에 복사했습니다 ({len(text):,}자)", "ok")
                    return
                except Exception:
                    pass
            if self._osc52_copy(text):
                msg = f"터미널 OSC52로 복사했습니다 ({len(text):,}자)"
                if saved:
                    msg += f" — 붙여넣기 안 되면 /copy share 로 공유 링크를 만드세요 ({saved})"
                self._notice(msg, "warn")
                return
            if saved:
                self._notice(f"복사 수단은 없지만 사본 저장: {saved}", "warn")
            else:
                self._notice("복사 실패: 클립보드 도구도 OSC52도 파일도 불가", "warn")

        def _duet_cmd(self, rest):
            """봇끼리 대화 — /duet <주제> 시작, /duet stop 중단."""
            if not (self._voice_on and self._voice):
                self._notice("먼저 /voice 로 음성방에 들어가세요 — 예: /duet 오늘 점심 뭐 먹을까", "warn")
                return
            arg = (rest or "").strip()
            if not arg:
                self._notice("사용법: /duet <주제> (중단: /duet stop)")
                return
            if arg.lower() in ("stop", "중단", "끝"):
                ok = self._voice.send_raw({"type": "duet", "stop": True})
                self._notice("봇 대화 중단 요청" if ok else "음성방 연결이 없습니다", "ok" if ok else "warn")
            else:
                ok = self._voice.send_raw({"type": "duet", "topic": arg})
                self._notice(f"봇 대화 시작 요청 — 주제: {arg}" if ok
                             else "음성방 연결이 없습니다", "ok" if ok else "warn")

        # ---- 음성방 (/voice) — 브라우저 마이크와 같은 대화 ----
        def _voice_toggle(self, rest=""):
            arg = (rest or "").strip().lower()
            if arg in ("off", "끄기", "stop"):
                if self._voice:
                    self._voice.stop()
                self._voice = None
                self._voice_on = False
                self._voice_live = None
                self._notice("음성방에서 나왔습니다")
                self._refresh_status()
                return
            if self._voice_on:
                self._notice("이미 음성방에 있습니다 — 나가려면 /voice off", "warn")
                return
            import re as _re
            import socket as _socket
            env_url = os.environ.get("VOICEHUB_WS", "ws://127.0.0.1:8096/ws")
            if arg and _re.match(r"^wss?://", arg):
                env_url = arg
            from harness.voicehub import VoiceRoomClient
            self._voice = VoiceRoomClient(env_url, who="터미널", on_event=self._voice_post)
            self._voice_on = True
            self._voice.start()
            page = env_url.replace("wss://", "https://").replace("ws://", "http://")
            page = page[: -len("/ws")] + "/" if page.endswith("/ws") else page
            self._notice(f"음성방 접속 중 — 같은 방 브라우저 페이지: {page} (나가기 /voice off)")
            self._refresh_status()

        def _voice_post(self, ev):
            """워커 스레드에서 온 사건을 UI 스레드로 안전하게 — 이미 UI 스레드면 바로."""
            import threading as _th
            if getattr(self, "_thread_id", None) == _th.get_ident():
                self._voice_event(ev)
            else:
                self.call_from_thread(self._voice_event, ev)

        def _voice_event(self, ev):
            if not self._voice_on:
                return
            kind = ev.get("type")
            if kind == "state":
                hist = ev.get("history") or []
                if hist:
                    lines = []
                    for h in hist[-40:]:
                        who = h.get("who") or ("AI" if h.get("role") == "assistant" else "")
                        lines.append(f"{who}: {h.get('text', '')}" if who else h.get("text", ""))
                    self._mount(AssistantMessage("[음성방 기록]\n" + "\n".join(lines)))
                else:
                    self._notice("음성방에 연결됐습니다 — 폰/PC 는 브라우저로 참여", "ok")
            elif kind == "user":
                self._voice_live = None
                self._voice_buf = ""
                self._voice_speaker = None
                who = ev.get("who") or "누군가"
                self._mount(UserMessage(f"🎤 {who}: {ev.get('text', '')}"))
            elif kind in ("ai_delta", "bot_delta"):
                who = str(ev.get("who") or "AI")
                if self._voice_speaker != who:
                    self._voice_speaker = who
                    self._voice_live = None
                    self._voice_buf = ""
                self._voice_buf += str(ev.get("text", ""))
                label = self._voice_label(who)
                if self._voice_live is None:
                    self._voice_live = Static(Text(label + self._voice_buf, style="", no_wrap=False),
                                              classes="assistant voice-live")
                    self._mount(self._voice_live)
                else:
                    try:
                        self._voice_live.update(Text(label + self._voice_buf))
                    except Exception:
                        pass
            elif kind in ("ai", "bot"):
                who = str(ev.get("who") or "AI")
                text = str(ev.get("text", "")) or self._voice_buf
                self._voice_buf = ""
                self._voice_speaker = None
                label = self._voice_label(who)
                if self._voice_live is not None:
                    try:
                        self._voice_live.update(Text(label + text))
                    except Exception:
                        pass
                    self._voice_live = None
                    return
                self._voice_live = None
                self._mount(AssistantMessage(label + text))
            elif kind == "duet":
                self._voice_live = None
                self._voice_buf = ""
                self._voice_speaker = None
                if ev.get("state") == "start":
                    self._notice(f"🤖 봇 대화 시작 — 주제: {ev.get('topic', '')} "
                                 f"({ev.get('bot', '봇')} vs 로이) · 중단: /duet stop", "ok")
                else:
                    self._notice("🤖 봇 대화 끝")
            elif kind == "interrupt":
                self._voice_live = None
                self._voice_buf = ""
                self._voice_speaker = None
                self._notice("🎤 끼어들기 — AI 답변이 중단됐습니다")
            elif kind == "status" and "끊김" in str(ev.get("text", "")):
                self._notice(str(ev.get("text")), "warn")

        @staticmethod
        def _voice_label(who):
            return "🔊 " if who == "AI" else f"🔊 {who}: "

        def _voice_send(self, text):
            if not self._voice or not self._voice.send(text):
                self._notice("음성방 연결이 없습니다 — /voice off 후 다시 /voice", "warn")
                return False
            return True

        # ---- 텍스트 붙여넣기 (Ctrl+V) ----
        def action_paste_text(self):
            text = clipboard_text()
            if not text:
                self._notice("클립보드에 텍스트가 없습니다 — 터미널 붙여넣기(Ctrl+Shift+V)를 써보세요", "warn")
                return
            flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
            if isinstance(self.focused, Input):
                target = self.focused
            elif len(self.screen_stack) == 1:
                target = self.query_one("#prompt", Input)
            else:
                return   # 모달이 열려 있으면 가려진 프롬프트에 넣지 않는다
            target.insert_text_at_cursor(flat)
            target.focus()

        # ---- 그림 첨부 (Ctrl+G · /image) ----
        def _inbox_dir(self):
            return os.path.join(self.cfg.workspace_root, "inbox")

        def action_paste_image(self):
            self._attach_clipboard()

        def _attach_clipboard(self):
            path, why = clipboard_image(self._inbox_dir())
            if not path:
                self._notice(f"{why} — /image <경로> 로 붙일 수 있습니다 (예: /image ~/shot.png). "
                             "다른 컴퓨터의 그림이면 서버로 옮긴 뒤 그 경로를 쓰세요", "warn")
                return
            self._attach_path(path)

        def _attach_path(self, raw):
            p = os.path.expanduser(str(raw).strip().strip("\"'"))
            if not os.path.isfile(p):
                self._notice(f"파일이 없습니다: {p}", "warn")
                return
            if not _is_image_name(p):
                self._notice(f"그림 파일이 아닙니다(.png/.jpg/.webp/.gif/.bmp): {p}", "warn")
                return
            real = os.path.realpath(p)
            root = os.path.realpath(self.cfg.workspace_root)
            if real != root and not real.startswith(root + os.sep):
                dest_dir = self._inbox_dir()
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, f"tui_{time.strftime('%Y%m%d_%H%M%S')}_{os.path.basename(real)}")
                shutil.copy2(real, dest)
                self._notice(f"작업폴더 밖 그림이라 inbox 로 복사했습니다: {dest}")
                real = dest
            if real not in self._attachment:
                self._attachment.append(real)
            self._notice(f"그림 첨부됨: {real} — 다음 메시지와 함께 전송됩니다", "ok")
            try:
                cols = self.size.width
            except Exception:
                cols = 80
            width = max(24, min(72, cols - 10))
            self._mount(ImagePreview(real, width, max(6, min(20, int(width * 0.35)))))
            self._mount_ocr(real)
            self._refresh_status()

        @work(thread=True, exclusive=False)
        def _mount_ocr(self, path):
            """그림 속 글자를 백그라운드에서 읽어(OCR) 창에 뿌린다."""
            text = ocr_text(path)
            if text:
                self.call_from_thread(self._mount, OcrText(path, text))
            else:
                self.call_from_thread(self._notice, f"OCR: {os.path.basename(path)} 에서 글자를 찾지 못했습니다")

        def _compose_task(self, task):
            """보낼 작업문 완성 — 대기 중 첨부 + 본문에 적힌 그림 경로를 모아 안내문을 붙인다."""
            atts = list(self._attachment)
            self._attachment = []
            for m in re.finditer(r"(?:~|\.{1,2}|/)[^\s\"'()]+\.(?:png|jpe?g|webp|gif|bmp)", task, re.I):
                p = os.path.realpath(os.path.expanduser(m.group(0)))
                if os.path.isfile(p) and _is_image_name(p) and p not in atts:
                    atts.append(p)
            if not atts:
                pc = re.search(r"[A-Za-z]:\\[^\n]*", task)
                if pc:
                    self._notice("PC(로컬) 경로는 이 서버에서 읽을 수 없습니다 — 그림을 서버로 옮긴 뒤 "
                                 "/image <경로> 로 붙이세요", "warn")
                return task, []
            note = "\n".join(f"[첨부 그림 {i + 1}] {p}" for i, p in enumerate(atts))
            full = (f"{task}\n\n{note}\n"
                    "(위 그림은 사용자가 방금 첨부한 로컬 파일이다. 그림 내용·글자가 필요하면 "
                    "image_describe/photo_doc/image_ocr 도구로 확인해 답하라.)")
            return full, atts

        def action_cancel_run(self):
            if self.busy:
                if self.agent:
                    self.agent.cancel()
                self._paused = False
                self.workers.cancel_all()
                self.busy = False
                self.handle_activity_end()
                self._notice("작업 취소 요청 — 진행 중 도구는 백그라운드에서 정리됩니다", "warn")
                self._refresh_status()
            else:
                try:
                    self.query_one("#prompt", Input).focus()
                except Exception:
                    pass

        def _pause_run(self):
            if not self.busy or not self.agent:
                self._notice("실행 중인 작업이 없습니다", "warn")
                return
            self.agent.pause()
            self._paused = True
            self._notice("일시정지 — 일반 입력으로 대화 추가 후 /resume", "ok")
            self._refresh_status()

        def _resume_run(self):
            self._paused = False
            if self.agent:
                self.agent.resume()
            self._notice("재개 — 작업을 계속합니다", "ok")
            self._refresh_status()

        def action_clear_log(self):
            log = self.query_one("#log", VerticalScroll)
            for child in list(log.children):
                if child.id != "welcome":
                    child.remove()
            self._notice("화면을 지웠습니다")

        def action_cycle_theme(self):
            try:
                themes = sorted(self.available_themes)
                cur = self.theme
                idx = themes.index(cur) if cur in themes else -1
                self.theme = themes[(idx + 1) % len(themes)]
                self._notice(f"테마: {self.theme}")
            except Exception as e:
                self._notice(f"테마 전환 실패: {e}", "warn")

        def action_model_picker(self):
            from harness import cli
            entries = cli.model_catalog()
            if not entries:
                self._notice("모델 목록을 가져올 수 없습니다 (ollama/vLLM 서버 연결 확인)", "warn")
                return
            cur = self.initial_model or self.cfg.model_fast
            self.push_screen(ModelPicker(entries, cur), self._on_model_picked)

        def _on_model_picked(self, entry):
            if not entry:
                return
            from harness import cli
            cfg = self.cfg
            cfg.base_url, cfg.api_key = entry["base_url"], entry["api_key"]
            cfg.model_fast = cfg.model_smart = entry["name"]
            # 로컬 thinking 모델은 추론이 출력 토큰을 다 삼켜 답이 잘린다.
            if cli._is_local(cfg.base_url) and not cfg.reasoning_effort:
                cfg.reasoning_effort = "none"
            self.initial_model = entry["name"]
            self._build_agent()
            self._notice(f"모델 변경 → {entry['name']} ({entry['source']})")
            self._refresh_status()

        def action_session_picker(self):
            files = _discover_sessions()
            self.push_screen(SessionPicker(files, self.cfg.session_file),
                             self._on_session_picked)

        def _on_session_picked(self, path):
            if not path:
                self._notice("세션 변경 취소")
                return
            self.cfg.session_file = path
            self._build_agent()
            self._load_view(path)
            self._notice(f"세션 재개 → {os.path.basename(path)}")

        def _load_view(self, path):
            log = self.query_one("#log", VerticalScroll)
            for child in list(log.children):
                if child.id != "welcome":
                    child.remove()
            for line in list(open(path, encoding="utf-8", errors="replace"))[-200:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                role = obj.get("role")
                content = obj.get("content") or ""
                if role == "user" and not content.startswith("[도구 결과]"):
                    if not content.startswith("[규칙") and not content.startswith("[피드백"):
                        self._mount(UserMessage(short(content, 400)))
                elif role == "assistant" and content.strip().startswith("{"):
                    try:
                        a = json.loads(content)
                    except ValueError:
                        continue
                    if a.get("done") and a.get("answer"):
                        self._mount(AssistantMessage(str(a["answer"])))

    return AutorcodeTUI


SLASH_HELP = {
    "/help": "도움말",
    "/model": "모델 선택기",
    "/sessions": "세션 브라우저",
    "/theme": "테마 전환",
    "/status": "상태",
    "/tools": "도구 목록",
    "/clear": "화면 지우기",
    "/auto": "자율 모드 on|off",
    "/approve": "프로젝트 전체 승인 on|off",
    "/pause": "실행 중인 작업 일시정지",
    "/resume": "일시정지된 작업 재개",
    "/cancel": "실행 중인 작업 취소",
    "/compact": "세션 요약·정리",
    "/copy": "선택 복사 (all=답변 전체, image=그림, share <경로>=공유링크)",
    "/image": "그림 첨부 (경로 생략 시 클립보드)",
    "/voice": "음성방 참여 on|off — 폰/PC 브라우저와 같은 대화",
    "/duet": "봇끼리 대화 시작 /duet <주제> · 중단 /duet stop",
    "/quit": "종료",
}


def _split_answer(answer):
    """agent.run 반환값을 (요약, 답변) 으로 나눈다."""
    if not isinstance(answer, str):
        return "", str(answer)
    if answer.startswith("[") and "\n" in answer:
        head, _, rest = answer.partition("\n")
        if head.startswith("[") and head.rstrip().endswith("]"):
            return head.strip(), rest.strip()
    return "", answer.strip()


def _discover_sessions():
    base = os.path.expanduser("~/.autorcode")
    files = set(glob.glob(os.path.join(base, "*.jsonl")))
    files |= set(glob.glob(os.path.join(base, "session_*.jsonl")))
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=lambda f: os.stat(f).st_mtime, reverse=True)
    return files


# ToolCard.compose 가 workspace 를 참조하므로 모듈 전역으로 둔다(단일 실행 전제).
_workspace_root = [os.getcwd()]

# 클래스는 처음 build() 호출 때 한 번 만든다.
AutorcodeTUI = None


def _ensure_app():
    global AutorcodeTUI, TuiProgress
    if AutorcodeTUI is None:
        AutorcodeTUI = _build_app_classes()
    return AutorcodeTUI


def run(cfg, model="", permissions=None):
    _require_textual()
    App = _ensure_app()
    App(cfg, model=model, permissions=permissions).run()
    return 0
