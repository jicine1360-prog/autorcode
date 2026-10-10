"""폰 비서 봇(bot.py) 테스트.

핵심 요점: getUpdates 폴러는 '봇 하나'여야 하고(409), 그 하나가 명령(작업)과
승인 버튼 callback 을 함께 처리한다. 승인은 gate.feed() 로 넘겨 기존 검증
(논스 1회용 · 화이트리스트 · chat 일치)을 그대로 쓴다.
"""
import json
import threading
import time
import unittest

from harness import bot, approve


class _Resp:
    def __init__(self, obj):
        self._obj = obj

    def read(self):
        return json.dumps(self._obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeApi:
    """Telegram API 대역. getUpdates 는 큐의 업데이트를 한 번만 돌려준다."""

    def __init__(self):
        self.calls = []
        self.answers = []
        self.queue = []

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        payload = json.loads(req.data or b"{}")
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.queue = self.queue, []
            return _Resp({"ok": True, "result": out})
        if method == "answerCallbackQuery":
            self.answers.append(payload.get("text", ""))
        return _Resp({"ok": True, "result": {"message_id": 1}})

    def sent_texts(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]


class FakeAgent:
    def __init__(self):
        self.runs = []

    def run(self, text):
        self.runs.append(text)
        return f"ANSWER({len(self.runs)})"


def _msg(chat, text, user=None):
    return {"update_id": 1, "message": {
        "message_id": 1,
        "chat": {"id": chat},
        "from": {"id": user or chat},
        "text": text,
    }}


def _cb(nonce, verdict="y", user=555, chat=555, cb_id="cb1"):
    return {"update_id": 2, "callback_query": {
        "id": cb_id, "data": f"ag:{verdict}:{nonce}",
        "from": {"id": user}, "message": {"message_id": 2, "chat": {"id": chat}},
    }}


def _await(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class MessageDispatchTest(unittest.TestCase):
    """메시지 → 작업 실행 → 같은 chat 으로 회신."""

    def setUp(self):
        self.api = FakeApi()
        self.agent = FakeAgent()
        self.b = bot.Bot("t", [555], opener=self.api,
                         agent_factory=lambda: self.agent)

    def test_work_runs_agent_and_replies(self):
        self.b._dispatch(_msg(555, "디스크 확인해줘"))
        self.assertTrue(_await(lambda: len(self.agent.runs) == 1))
        self.assertIn("ANSWER(1)", self.api.sent_texts()[-1])
        # 회신은 명령을 보낸 chat 으로 간다
        self.assertEqual(self.api.calls[-1][1]["chat_id"], 555)

    def test_non_allowed_chat_ignored(self):
        self.b._dispatch(_msg(999, "실행"))
        time.sleep(0.1)
        self.assertEqual(self.agent.runs, [])
        self.assertEqual(self.api.sent_texts(), [])

    def test_busy_rejects_second(self):
        self.b._busy = True
        self.b._dispatch(_msg(555, "두 번째 작업"))
        time.sleep(0.1)
        self.assertEqual(self.agent.runs, [])
        self.assertTrue(any("작업 중" in t for t in self.api.sent_texts()))

    def test_help_command(self):
        self.b._dispatch(_msg(555, "/help"))
        time.sleep(0.1)
        self.assertIn("작업 지시로 실행", "".join(self.api.sent_texts()))

    def test_status_command(self):
        self.b._dispatch(_msg(555, "/status"))
        time.sleep(0.1)
        self.assertIn("autorcode 일일 리포트", "".join(self.api.sent_texts()))


class ApprovalViaBotTest(unittest.TestCase):
    """봇이 승인 callback 을 게이트로 넘겨 기존 검증을 그대로 쓴다."""

    def setUp(self):
        self.api = FakeApi()
        self.gate = approve.ApprovalGate("t", [555], timeout=2, opener=self.api)
        self.b = bot.Bot("t", [555], gate=self.gate, opener=self.api)

    def _ask(self):
        result = {}

        def ask():
            result["verdict"] = self.gate("rclone move --include='*.secret' /in /out")
        self.gate.current_chat = 555
        t = threading.Thread(target=ask, daemon=True)
        t.start()
        # 승인 요청이 나가 nonce 를 뽑는다
        self.assertTrue(_await(lambda: any(
            m == "sendMessage" and p.get("reply_markup") for m, p in self.api.calls)))
        for m, p in self.api.calls:
            if m == "sendMessage" and p.get("reply_markup"):
                data = p["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                return result, t, data.split(":")[-1]
        raise AssertionError("승인 요청 메시지를 찾지 못함")

    def test_allow_via_bot(self):
        result, t, nonce = self._ask()
        self.b._dispatch(_cb(nonce, "y"))
        t.join(timeout=5)
        self.assertTrue(result["verdict"])
        self.assertIn("실행합니다", self.api.answers)

    def test_reject_via_bot(self):
        result, t, nonce = self._ask()
        self.b._dispatch(_cb(nonce, "n"))
        t.join(timeout=5)
        self.assertFalse(result["verdict"])
        self.assertIn("거부했습니다", self.api.answers)

    def test_unauthorized_user_rejected(self):
        result, t, nonce = self._ask()
        self.b._dispatch(_cb(nonce, "y", user=777, chat=555))
        self.assertIn("승인 권한이 없습니다", self.api.answers)
        # 승인은 허용된 사용자로 다시 눌러야 성립한다
        self.b._dispatch(_cb(nonce, "y", user=555, chat=555))
        t.join(timeout=5)
        self.assertTrue(result["verdict"])

    def test_nonce_consumed_once(self):
        result, t, nonce = self._ask()
        self.b._dispatch(_cb(nonce, "y"))
        t.join(timeout=5)
        self.b._dispatch(_cb(nonce, "y", cb_id="cb2"))
        self.assertIn("이미 처리된 요청입니다", self.api.answers)


class BuildBotTest(unittest.TestCase):
    def test_missing_config_returns_none(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(bot.build_bot(path=os.path.join(d, "nope.json")))

    def test_prefers_dedicated_bot_config(self):
        import os
        import tempfile
        from harness.approve import load_config
        with tempfile.TemporaryDirectory() as d:
            bot2 = os.path.join(d, "bot.json")
            tele = os.path.join(d, "telegram.json")
            for path, tok in ((bot2, "999:AA"), (tele, "555:AA")):
                with open(path, "w", encoding="utf-8") as f:
                    import json
                    json.dump({"token": tok, "chat": "555"}, f)
                os.chmod(path, 0o600)
            old = bot.BOT_CONFIG
            bot.BOT_CONFIG = bot2
            try:
                b = bot.build_bot()
                self.assertIsNotNone(b)
                self.assertEqual(b._token, "999:AA")
            finally:
                bot.BOT_CONFIG = old

    def test_env_token_preferred(self):
        import os
        old = {k: os.environ.get(k) for k in ("AGENTUPBOT_TOKEN", "AGENTUPBOT_CHAT")}
        os.environ["AGENTUPBOT_TOKEN"] = "777:ENV"
        os.environ["AGENTUPBOT_CHAT"] = "50735853"
        try:
            b = bot.build_bot()
            self.assertIsNotNone(b)
            self.assertEqual(b._token, "777:ENV")
            self.assertEqual(list(b._allowed), [50735853])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()