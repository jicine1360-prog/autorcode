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
  슬래시     /help /model /sessions /theme /status /tools /clear /auto /quiet /quit
"""
import difflib
import glob
import json
import os
import threading
import time
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
        Button, Collapsible, Input, Markdown, OptionList, Static,
    )
    from textual.widgets.option_list import Option
    from rich.text import Text

    # ---------------- 위젯 ----------------
    class Notice(Static):
        def __init__(self, text, kind="info"):
            super().__init__(Text(clean(text), style="dim"), classes=f"notice {kind}")

    class UserMessage(Static):
        def __init__(self, text):
            super().__init__(Text("› " + text, style="bold"), classes="user")

    class AssistantMessage(Markdown):
        def __init__(self, text):
            super().__init__(text, classes="assistant")

    class ToolCard(Vertical):
        """도구 실행 카드 — 헤더(아이콘·라벨·상태·시간) + 본문(결과/diff).

        결과는 상태로만 저장하고 compose 에서 그린다. 도구가 빨리 끝나
        mount 직후 result 가 오면 자식이 아직 안 만들어졌을 수 있으므로
        query_one 대신 refresh(recompose=True) 로 다시 그린다.
        """

        def __init__(self, name, args, label):
            super().__init__(classes="tool")
            self.tool_name = name
            self.tool_args = args or {}
            self.tool_label = label
            self._result = None
            self._elapsed = 0.0
            self._old_content = self._read_old(name, self.tool_args)

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

        def _head(self):
            icon = _TOOL_ICON.get(self.tool_name, "•")
            if self._result is None:
                return Text(f"  {icon} {short(self.tool_label, 120)}", style="bold")
            status = result_status(self._result)
            style = _STATUS_STYLE.get(status, "green")
            return Text.assemble(
                (f"  {icon} ", "bold"),
                (short(self.tool_label, 120), ""),
                (f"   [{status}] {self._elapsed:.1f}s · {len(self._result):,}자", style),
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
                return [Collapsible(Static(diff), title="변경 diff", collapsed=False)]
            lines = [ln for ln in clean(self._result).splitlines() if ln.strip()]
            preview = "\n".join(lines[:12]) or self._result
            status = result_status(self._result)
            return [Collapsible(Static(Text(preview, style="dim")), title="결과 보기",
                                collapsed=(status != "완료"))]

        def compose(self) -> ComposeResult:
            yield Static(self._head(), classes="tool-head")
            yield from self._body_widgets()

        def set_result(self, result, elapsed):
            self._result = result
            self._elapsed = elapsed
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
        BINDINGS = [Binding("escape", "cancel", "취소")]

        def __init__(self, names, current):
            super().__init__()
            self.names = names
            self.current = current

        def compose(self) -> ComposeResult:
            with Vertical(classes="dialog"):
                yield Static("모델 선택 (Enter)", classes="dialog-title")
                opts = [Option(("● " if n == self.current else "  ") + n, id=n)
                        for n in self.names]
                yield OptionList(*opts, id="models")

        def on_mount(self):
            self.query_one("#models", OptionList).focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected):
            self.dismiss(event.option.id)

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
        #slashhint { height: 1; color: $accent; padding: 0 2; }
        #prompt { dock: bottom; }
        .user { color: $text; margin: 1 0 0 0; }
        .assistant { margin: 0 0 1 0; }
        .notice { color: $text-muted; }
        .notice.error { color: $error; }
        .notice.warn { color: $warning; }
        .notice.ok { color: $success; }
        .tool { border: round $primary 40%; margin: 0 0 0 0; padding: 0; }
        .tool-head { color: $text; }
        .tool-body { padding: 0 1; }
        .dialog { width: 80%; max-width: 100; height: auto; max-height: 80%;
                  border: thick $accent; background: $surface; padding: 1 2; }
        .dialog-title { text-style: bold; padding: 0 0 1 0; }
        .dialog-body { padding: 0 0 1 0; }
        .dialog-buttons { height: auto; }
        """
        BINDINGS = [
            Binding("ctrl+q", "quit_app", "종료"),
            Binding("ctrl+c", "quit_app", "종료", priority=True),
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

        # ---- 수명주기 ----
        def compose(self) -> ComposeResult:
            yield Static(id="status")
            with VerticalScroll(id="log"):
                yield Static(id="welcome")
            yield Static(id="activity")
            yield Static(id="slashhint")
            yield Input(placeholder="메시지 입력  (/help · F2 모델 · F3 세션 · Ctrl+T 테마)",
                        id="prompt")

        def on_mount(self):
            self.title = "autorcode"
            self._workspace_ref()
            self._build_agent()
            log = self.query_one("#log", VerticalScroll)
            log.query_one("#welcome").update(Text.assemble(
                ("  autorcode TUI\n", "bold"),
                (f"  모델 {self.cfg.model_fast}  ·  작업 위치 {self.cfg.workspace_root}\n", "dim"),
                ("  Enter 전송 · Ctrl+P 팔레트 · F2 모델 · F3 세션 · Ctrl+T 테마\n", "dim"),
            ))
            self.query_one("#prompt", Input).focus()
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

        def run(self, *a, **kw):
            _workspace_root[0] = self.cfg.workspace_root
            return super().run(*a, **kw)

        # ---- 승인 ----
        def _confirmer(self, why):
            ev = threading.Event()
            box = {}
            self.call_from_thread(self._push_approve, why, ev, box)
            ev.wait()
            return bool(box.get("ok", False))

        def _push_approve(self, why, ev, box):
            def done(ok):
                box["ok"] = bool(ok)
                ev.set()
            try:
                self.push_screen(ApproveScreen(why), done)
            except Exception:
                box["ok"] = False
                ev.set()

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
            self._render_activity()

        def handle_activity_update(self, message):
            self._recv = message
            self._render_activity()

        def handle_activity_end(self):
            self._activity = None
            self._recv = ""
            self._render_activity()

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
            perm = self.cfg.permissions_mode + ("/auto" if self.cfg.auto_yes else "")
            right = f"{step_s} · 권한 {perm}".strip()
            w.update(Text.assemble(
                (" autorcode ", "bold reverse"),
                (f"  {model} ", "bold"),
                (f"[{tier}] ", "dim"),
                (f"· {ws}", "dim"),
                (f"   {right}", "dim"),
            ))

        # ---- 입력 ----
        def on_input_changed(self, event: Input.Changed):
            hint = self.query_one("#slashhint", Static)
            val = event.value
            if val.startswith("/"):
                matches = [c for c in SLASH_HELP if c.startswith(val)]
                if matches:
                    hint.update(Text("  " + "   ".join(f"{c} {SLASH_HELP[c]}" for c in matches),
                                     style="dim"))
                else:
                    hint.update(Text("  알 수 없는 명령 — /help", style="dim"))
            else:
                hint.update("")

        def on_input_submitted(self, event: Input.Submitted):
            task = event.value.strip()
            self.query_one("#prompt", Input).value = ""
            self.query_one("#slashhint", Static).update("")
            if not task:
                return
            if task.startswith("/"):
                self._slash(task)
                return
            if self.busy:
                self._notice("이미 작업이 실행 중입니다 — 끝나면 보내세요", "warn")
                return
            self._mount(UserMessage(task))
            self.busy = True
            self._run_task(task)

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

        def action_cancel_run(self):
            if self.busy:
                self.workers.cancel_all()
                self.busy = False
                self.handle_activity_end()
                self._notice("작업 취소 요청 — 진행 중 도구는 백그라운드에서 정리됩니다", "warn")
            else:
                try:
                    self.query_one("#prompt", Input).focus()
                except Exception:
                    pass

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
            names = cli._names()
            if not names:
                self._notice("모델 목록을 가져올 수 없습니다 (ollama 연결 확인)", "warn")
                return
            cur = self.initial_model or self.cfg.model_fast
            self.push_screen(ModelPicker(names, cur), self._on_model_picked)

        def _on_model_picked(self, name):
            if not name:
                return
            from harness import cli
            cfg = self.cfg
            base_url, api_key = cli._resolve_backend(name)
            cfg.base_url, cfg.api_key = base_url, api_key
            cfg.model_fast = cfg.model_smart = name
            self.initial_model = name
            self._build_agent()
            self._notice(f"모델 변경 → {name}")
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
