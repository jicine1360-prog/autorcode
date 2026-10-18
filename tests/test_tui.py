"""TUI (Textual) 테스트 — Textual 이 없으면 통째로 건너뛴다.

CI 기본 환경에는 textual 이 없으므로 skip 된다. 로컬에서
`pip install textual` 후 돌리면 헤드리스 파일럿으로 실제 위젯 흐름을 검증한다.
"""
import asyncio
import os
import tempfile
import threading
import unittest

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


def _cfg(auto_yes=True):
    from harness.config import Config
    cfg = Config()
    cfg.base_url = ""          # mock 모델
    cfg.api_key = ""
    cfg.workspace_root = tempfile.mkdtemp()
    cfg.session_file = ""
    cfg.auto_yes = auto_yes
    return cfg


@unittest.skipUnless(HAS_TEXTUAL, "textual 미설치")
class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def _app(self, auto_yes=True):
        from harness import tui
        App = tui._ensure_app()
        return App(_cfg(auto_yes), model="mock:1")

    async def _settle(self, pilot, app, tries=60):
        for _ in range(tries):
            await pilot.pause(0.1)
            if not app.busy:
                return
        self.fail("작업이 끝나지 않음")

    async def test_mock_task_shows_toolcard_and_answer(self):
        from textual.widgets import Static
        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#prompt").value = "파일 목록 봐"
            await pilot.press("enter")
            await self._settle(pilot, app)
            await pilot.pause(0.3)
            kinds = [type(c).__name__ for c in app.query_one("#log").children]
            self.assertIn("UserMessage", kinds)
            self.assertIn("ToolCard", kinds)
            self.assertIn("AssistantMessage", kinds)

    async def test_stream_live_text_and_json_suppressed(self):
        """수신 글자가 로그에 실시간으로 뜨고, JSON 계획은 안 뿌리고, thought 는 배지로 확정한다."""
        from harness.progress import Activity
        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.2)

            def text_of(c):
                src = getattr(c, "source", None)
                if src:
                    return str(src)
                try:
                    return str(c.render())
                except Exception:
                    return ""

            def texts():
                return [text_of(c) for c in app.query_one("#log").children]

            app.handle_text_delta("안")
            await pilot.pause(0.15)  # 리렌더 스로틀(80ms) 통과
            app.handle_text_delta("녕하세요")
            await pilot.pause(0.2)
            self.assertTrue(any("안녕하세요" in t for t in texts()))

            # thought 도착 → StepLine 중복 없이 같은 블록이 단계 배지로 확정(전문 유지)
            app.handle_thought(1, "안녕하세요")
            await pilot.pause(0.2)
            children = app.query_one("#log").children
            self.assertEqual([type(c).__name__ for c in children].count("StepLine"), 0)
            self.assertTrue(any("◆ 1단계" in t and "안녕하세요" in t for t in texts()))

            # JSON 계획 응답은 실시간으로 뿌리지 않는다
            app.handle_activity_start("모델 응답 대기", Activity("t"))
            app.handle_text_delta('{"thought": "x", "actions": []}')
            await pilot.pause(0.2)
            self.assertFalse(any('"actions"' in t for t in texts()))

            # 재시도 리셋 — 흘리던 조각은 사라진다
            app.handle_activity_start("모델 응답 대기", Activity("t"))
            app.handle_text_delta("부분 텍스트")
            await pilot.pause(0.1)
            app.handle_text_reset()
            await pilot.pause(0.1)
            self.assertFalse(any("부분 텍스트" in t for t in texts()))

            # 평문 답변: 실시간으로 흘린 뒤 완료되면 마크다운 하나로 정리된다(중복 없음)
            app.handle_activity_start("모델 응답 대기", Activity("t"))
            app.handle_text_delta("지금 상태는 정상입니다")
            await pilot.pause(0.2)
            app.handle_activity_end()
            app._finish_task("지금 상태는 정상입니다")
            await pilot.pause(0.2)
            joined = " ".join(texts())
            self.assertEqual(joined.count("지금 상태는 정상입니다"), 1)

    async def test_slash_tools_lists_tools(self):
        from textual.widgets import Static
        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#prompt").value = "/tools"
            await pilot.press("enter")
            await pilot.pause(0.2)
            notices = [str(c.render()) for c in app.query_one("#log").children
                       if type(c).__name__ == "Notice"]
            self.assertTrue(any("도구" in n for n in notices))

    async def test_approval_modal(self):
        async def one(key, expected):
            app = await self._app(auto_yes=False)
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.pause(0.3)
                box = {}

                def worker():
                    box["ok"] = app._confirmer("테스트 승인 요청")

                t = threading.Thread(target=worker, daemon=True)
                t.start()
                seen = False
                for _ in range(50):
                    await pilot.pause(0.05)
                    if type(app.screen).__name__ == "ApproveScreen":
                        seen = True
                        break
                self.assertTrue(seen, "승인 모달이 뜨지 않음")
                await pilot.press(key)
                t.join(timeout=5)
                self.assertEqual(box.get("ok"), expected)

        await one("y", True)
        await one("n", False)

    async def test_model_picker_opens_and_cancels(self):
        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            app.action_model_picker()
            await pilot.pause(0.2)
            # ollama 미연결이면 안내만; 연결됐다면 ModelPicker
            name = type(app.screen).__name__
            if name == "ModelPicker":
                await pilot.press("escape")
                await pilot.pause(0.1)
            self.assertIn(name, ("ModelPicker", "Screen"))

    async def test_image_attach_preview_and_directive(self):
        """'/image <경로>' → 미리보기 카드 + 다음 메시지에 [첨부] 안내문이 들어간다."""
        import base64
        import os
        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            path = os.path.join(app.cfg.workspace_root, "shot.png")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(ONE_PX_PNG))
            app.query_one("#prompt").value = f"/image {path}"
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertEqual(app._attachment, [path])
            kinds = [type(c).__name__ for c in app.query_one("#log").children]
            self.assertIn("ImagePreview", kinds)

            app.query_one("#prompt").value = "그림 설명해줘"
            await pilot.press("enter")
            await self._settle(pilot, app)
            await pilot.pause(0.3)
            users = [str(c.render()) for c in app.query_one("#log").children
                     if type(c).__name__ == "UserMessage"]
            self.assertTrue(any("[첨부" in u for u in users))
            self.assertTrue(any("image_describe" in str(m.get("content", ""))
                                for m in app.agent.mem.turns))
            self.assertEqual(app._attachment, [])

    async def test_ctrl_v_attaches_clipboard_image(self):
        import base64
        import os
        from unittest import mock
        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            path = os.path.join(app.cfg.workspace_root, "clip.png")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(ONE_PX_PNG))
            from harness import tui as tui_mod
            with mock.patch.object(tui_mod, "clipboard_image", return_value=(path, "")):
                await pilot.press("ctrl+v")
                await pilot.pause(0.3)
            self.assertEqual(app._attachment, [path])
            kinds = [type(c).__name__ for c in app.query_one("#log").children]
            self.assertIn("ImagePreview", kinds)

    async def test_voice_room_toggle_and_routing(self):
        """/voice → 같은 방 이벤트가 자막으로 뜨고, 입력은 방으로(에이전트 아님) 간다."""
        from unittest import mock

        class FakeClient:
            last = None

            def __init__(self, url, who="터미널", on_event=None):
                self.who = who
                self.on_event = on_event
                self.stopped = False
                FakeClient.last = self

            def start(self):
                self.on_event({"type": "state", "history": [
                    {"role": "user", "who": "웹", "text": "폰에서 한 말", "ts": 1}]})

            def send(self, text):
                self.on_event({"type": "user", "who": self.who, "text": text, "ts": 2})
                self.on_event({"type": "ai_delta", "text": "반가"})
                self.on_event({"type": "ai", "text": "반가워요", "audio": "/tts/x.mp3"})
                return True

            def send_raw(self, obj):
                self.raw = getattr(self, "raw", [])
                self.raw.append(obj)
                return True

            def stop(self):
                self.stopped = True

        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            with mock.patch("harness.voicehub.VoiceRoomClient", FakeClient):
                app.query_one("#prompt").value = "/voice"
                await pilot.press("enter")
                await pilot.pause(0.3)
            self.assertTrue(app._voice_on)
            app.query_one("#prompt").value = "터미널에서 말"
            await pilot.press("enter")
            await pilot.pause(0.3)

            def text_of(c):
                src = getattr(c, "source", None)
                if src:
                    return str(src)
                try:
                    return str(c.render())
                except Exception:
                    return ""

            texts = [text_of(c) for c in app.query_one("#log").children]
            self.assertTrue(any("폰에서 한 말" in t for t in texts))
            self.assertTrue(any("🎤 터미널: 터미널에서 말" in t for t in texts))
            self.assertTrue(any("반가워요" in t for t in texts))
            self.assertFalse(app.busy)
            app._slash("/voice off")
            await pilot.pause(0.2)
            self.assertFalse(app._voice_on)
            self.assertTrue(FakeClient.last.stopped)

    async def test_duet_command_and_bot_subtitles(self):
        """/duet → 방에 시작 요청, 봇 발언이 이름표 붙은 자막으로 뜨고 stop 은 중단 요청."""
        from unittest import mock

        class FakeClient:
            last = None

            def __init__(self, url, who="터미널", on_event=None):
                self.who = who
                self.on_event = on_event
                self.raw = []
                FakeClient.last = self

            def start(self):
                pass

            def send(self, text):
                return True

            def send_raw(self, obj):
                self.raw.append(obj)
                return True

            def stop(self):
                pass

        app = await self._app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.3)
            with mock.patch("harness.voicehub.VoiceRoomClient", FakeClient):
                app.query_one("#prompt").value = "/voice"
                await pilot.press("enter")
                await pilot.pause(0.2)
                app._slash("/duet 점심 메뉴 추천")
                await pilot.pause(0.2)
                client = FakeClient.last
                self.assertEqual(client.raw[-1], {"type": "duet", "topic": "점심 메뉴 추천"})
                client.on_event({"type": "duet", "state": "start",
                                 "topic": "점심 메뉴 추천", "bot": "건우"})
                client.on_event({"type": "duet_turn", "who": "건우"})
                client.on_event({"type": "bot_delta", "who": "건우", "text": "저는"})
                client.on_event({"type": "bot", "who": "건우", "text": "저는 김치찌개요",
                                 "audio": "/tts/g.mp3", "duet": True})
                client.on_event({"type": "duet", "state": "end", "topic": "점심 메뉴 추천"})
                await pilot.pause(0.3)
                app._slash("/duet stop")
                await pilot.pause(0.2)
                self.assertEqual(client.raw[-1], {"type": "duet", "stop": True})

            def text_of(c):
                src = getattr(c, "source", None)
                if src:
                    return str(src)
                try:
                    return str(c.render())
                except Exception:
                    return ""

            texts = [text_of(c) for c in app.query_one("#log").children]
            self.assertTrue(any("봇 대화 시작" in t for t in texts))
            self.assertTrue(any("🔊 건우: 저는 김치찌개요" in t for t in texts))
            self.assertTrue(any("봇 대화 끝" in t for t in texts))


ONE_PX_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
              "hKmMIQAAAABJRU5ErkJggg==")


class ImageAttachHelperTests(unittest.TestCase):
    """클립보드/미리보기 헬퍼 — Textual 없이도 돈다."""

    def test_no_clipboard_tools_reports_reason(self):
        from unittest import mock
        from harness import tui
        with mock.patch("shutil.which", lambda name: None):
            with tempfile.TemporaryDirectory() as d:
                path, why = tui.clipboard_image(d)
        self.assertIsNone(path)
        self.assertIn("도구", why)

    def test_clipboard_png_bytes_are_saved(self):
        import base64
        from unittest import mock
        from harness import tui
        png = base64.b64decode(ONE_PX_PNG)

        class R:
            returncode = 0
            stdout = png
            stderr = b""

        with mock.patch("shutil.which", lambda name: "/usr/bin/" + name), \
             mock.patch("subprocess.run", lambda *a, **k: R()):
            with tempfile.TemporaryDirectory() as d:
                path, why = tui.clipboard_image(d)
                self.assertTrue(path and os.path.isfile(path), why)
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), png)
                self.assertTrue(path.endswith(".png"))

    def test_image_block_text_draws_half_blocks(self):
        import base64
        from harness import tui
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.png")
            with open(p, "wb") as fh:
                fh.write(base64.b64decode(ONE_PX_PNG))
            text = tui.image_block_text(p, 10, 4)
            self.assertIsNotNone(text)
            self.assertIn("▀", str(text))

    async def test_model_picker_select_switches_backend(self):
        """선택한 엔트리의 백엔드(엔드포인트·키)가 그대로 cfg 로 반영돼야 한다."""
        from unittest.mock import patch
        entries = [{"name": "qwen3.6-35b-nvfp4",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": "dummy", "source": "127.0.0.1:8000"}]
        app = await self._app()
        with patch("harness.cli.model_catalog", return_value=entries):
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.pause(0.3)
                app.action_model_picker()
                await pilot.pause(0.2)
                self.assertEqual(type(app.screen).__name__, "ModelPicker")
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertEqual(app.cfg.base_url, "http://127.0.0.1:8000/v1")
                self.assertEqual(app.cfg.api_key, "dummy")
                self.assertEqual(app.cfg.model_fast, "qwen3.6-35b-nvfp4")
                self.assertEqual(app.initial_model, "qwen3.6-35b-nvfp4")
                notices = [str(c.render()) for c in app.query_one("#log").children
                           if type(c).__name__ == "Notice"]
                self.assertTrue(any("모델 변경" in n for n in notices))
