"""voicehub 서버 스피커(play_local) 단위 테스트 — 가짜 Popen 으로 흐름만 검증."""
import os
import subprocess
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import voicehub as vh

calls = []


class FakePopen:
    def __init__(self, cmd, **kw):
        calls.append(cmd)
        self.stdin = types.SimpleNamespace(
            write=lambda b: calls.append(("write", len(b))),
            close=lambda: calls.append("close"))

    def poll(self):
        return None  # 아직 실행 중 → stop_local 이 terminate 를 걸러야 한다

    def terminate(self):
        calls.append("terminate")

    # 실제 subprocess.Popen 은 context manager 다. 이 가짜가 뒤집어쓴 동안에도
    # 다른 코드가 `with subprocess.Popen(...)` 를 만나면 TypeError 가 나지
    # 않게 같은 프로토콜을 제공한다.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.terminate()
        return False


class SpeakerTest(unittest.TestCase):
    """가짜 Popen 은 테스트 안에서만 갈아끼운다.

    모듈 최상단에서 subprocess.Popen 을 덮어쓰면 되돌리는 곳이 없어, 그 뒤에
    import 되는 테스트 전부가 가짜 Popen 으로 실행돼 연쇄 파손된다 (pytest 는
    collect 시점에 import 하므로 체감 증상이 더 기괴해진다).
    """

    def setUp(self):
        calls.clear()
        self._popen = subprocess.Popen
        subprocess.Popen = FakePopen
        self.addCleanup(setattr, subprocess, "Popen", self._popen)
        self._env = dict(os.environ)
        self.addCleanup(os.environ.clear)
        self.addCleanup(os.environ.update, self._env)

    def test_card_auto_select(self):
        os.environ["VOICEHUB_MOCK"] = "1"
        os.environ["VOICEHUB_SPEAKER"] = "auto"
        card = vh.Hub(vh.Config(mock=True))._speaker_card()
        if not card:
            self.skipTest("재생 가능한 ALSA 카드가 없는 머신 — 자동선택 검증 불가")
        self.assertTrue(card.startswith("hw:"), card)

    def test_play_local_feeds_and_terminates(self):
        os.environ["VOICEHUB_MOCK"] = "1"
        os.environ["VOICEHUB_SPEAKER"] = "auto"
        hub = vh.Hub(vh.Config(mock=True))
        if not hub._speaker_card():
            self.skipTest("재생 가능한 ALSA 카드가 없는 머신 — 실행 경로 검증 불가")
        hub.play_local(b"FAKEMP3" * 10)
        time.sleep(0.3)
        self.assertTrue(any(isinstance(c, tuple) and c[0] == "write" for c in calls),
                        calls)  # mp3 전송 없음
        hub.stop_local()
        self.assertIn("close", calls)
        self.assertIn("terminate", calls)

    def test_speaker_off_does_not_play(self):
        hub = vh.Hub(vh.Config(mock=True, speaker="off"))
        self.assertEqual(hub._speaker_card(), "")
        hub.play_local(b"x")
        self.assertIsNone(hub._speaker_proc)

    def test_card_can_be_given_directly(self):
        hub = vh.Hub(vh.Config(mock=True, speaker="hw:1,7"))
        self.assertEqual(hub._speaker_card(), "hw:1,7")


if __name__ == "__main__":
    unittest.main()
