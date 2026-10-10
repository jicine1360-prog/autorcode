"""폰 위치 공유 — 저장·조회 + 봇의 위치 요청(키보드)·대기 질문 재실행 테스트.

네트워크 없이: geo._get 을 역지오코딩 스텁으로, 봇은 _call 을 기록 스텁으로 한다.
"""
import os
import shutil
import tempfile
import time
import unittest

from harness import bot as bot_mod
from harness import geo, location as loc, study


class LocationStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AUTORCODE_LOCATION_DIR"] = self.tmp
        self._real_get = geo._get
        geo._get = lambda url, timeout=30: {
            "display_name": "대한민국, 서울특별시, 강남구, 언주로 123"}
        self.chat = 50735853

    def tearDown(self):
        geo._get = self._real_get
        os.environ.pop("AUTORCODE_LOCATION_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_load_describe(self):
        rec = loc.store(self.chat, 37.5173, 127.0473)
        self.assertIn("강남구", rec["name"])
        self.assertEqual(loc.load(self.chat), rec)
        self.assertIn("37.51730", loc.describe(self.chat))
        self.assertIn("강남구", loc.describe(self.chat))

    def test_load_missing_returns_none(self):
        self.assertIsNone(loc.load(999))

    def test_per_chat_separated(self):
        loc.store(self.chat, 37.5, 127.0)
        self.assertIsNone(loc.load(self.chat + 1))


class _Fake:
    def __init__(self):
        self.calls = []

    def run(self, task):
        self.calls.append(task)
        return "ok-답"


def _wait(cond, until=3.0):
    t0 = time.time()
    while time.time() - t0 < until:
        if cond():
            return True
        time.sleep(0.02)
    return False


class BotLocationFlowTest(LocationStoreTest):
    def setUp(self):
        super().setUp()
        self.agent = _Fake()
        self.sent = []
        self.bot = bot_mod.Bot("t", [self.chat], opener=self._ok,
                               agent_factory=lambda: self.agent)
        self.bot._call = self._record

    def _ok(self, req, timeout=40):  # 사용 안 함 — _call 교체로 대체
        raise AssertionError("opener 불필요")

    def _record(self, method, payload):
        self.sent.append((method, payload))
        return {}

    def _last(self):
        return self.sent[-1][1] if self.sent else {}

    def test_asks_location_when_missing(self):
        self.bot._handle(self.chat, "근처 카페 알려줘")
        self.assertEqual(self.bot._pending, {str(self.chat): "근처 카페 알려줘"})
        self.assertFalse(self.agent.calls)
        markup = self._last().get("reply_markup", {})
        self.assertTrue(markup.get("keyboard"))

    def test_location_message_stores_and_reruns_pending(self):
        self.bot._handle(self.chat, "근처 카페 알려줘")  # 위치 없음 → 요청 + 대기
        self.assertEqual(self.bot._pending, {str(self.chat): "근처 카페 알려줘"})
        self.bot._on_location(self.chat, {"latitude": 37.5173, "longitude": 127.0473})
        self.assertTrue(_wait(lambda: self.agent.calls))
        self.assertEqual(self.bot._pending, {})
        self.assertEqual(loc.load(self.chat)["lat"], 37.5173)
        self.assertIn("[이 사용자의 최근 위치]", self.agent.calls[0])
        self.assertIn("강남구", self.agent.calls[0])

    def test_no_ask_when_location_exists(self):
        loc.store(self.chat, 37.5, 127.0)
        self.bot._handle(self.chat, "근처 카페")
        self.assertTrue(_wait(lambda: self.agent.calls))
        self.assertEqual(self.bot._pending, {})
        self.assertNotIn("reply_markup", self._last())

    def test_loc_intent_keywords_only_when_missing(self):
        self.assertTrue(self.bot._loc_intent("위치 보내줘"))
        self.assertTrue(self.bot._loc_intent("근처 맛집"))
        self.assertFalse(self.bot._loc_intent("안녕하세요"))

    def test_study_hint_in_preamble(self):
        loc.store(self.chat, 37.5, 127.0)
        study.save(self.chat, "전기차", "LFP vs NCM")
        self.bot._handle(self.chat, "아까 그거 더 알려줘")
        self.assertTrue(_wait(lambda: self.agent.calls))
        self.assertIn("준비된 학습 자료", self.agent.calls[0])


if __name__ == "__main__":
    unittest.main()