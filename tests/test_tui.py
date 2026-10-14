"""TUI (Textual) 테스트 — Textual 이 없으면 통째로 건너뛴다.

CI 기본 환경에는 textual 이 없으므로 skip 된다. 로컬에서
`pip install textual` 후 돌리면 헤드리스 파일럿으로 실제 위젯 흐름을 검증한다.
"""
import asyncio
import tempfile
import threading
import unittest

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


def _cfg():
    from harness.config import Config
    cfg = Config()
    cfg.base_url = ""          # mock 모델
    cfg.api_key = ""
    cfg.workspace_root = tempfile.mkdtemp()
    cfg.session_file = ""
    cfg.auto_yes = True
    return cfg


@unittest.skipUnless(HAS_TEXTUAL, "textual 미설치")
class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def _app(self):
        from harness import tui
        App = tui._ensure_app()
        return App(_cfg(), model="mock:1")

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
            app = await self._app()
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
