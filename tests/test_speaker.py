"""voicehub 서버 스피커(play_local) 단위 테스트 — 가짜 Popen 으로 흐름만 검증."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["VOICEHUB_MOCK"] = "1"
os.environ["VOICEHUB_SPEAKER"] = "auto"

import subprocess as _sp
from harness import voicehub as vh

calls = []


class FakePopen:
    def __init__(self, cmd, **kw):
        calls.append(cmd)
        self.stdin = types.SimpleNamespace(
            write=lambda b: calls.append(("write", len(b))),
            close=lambda: calls.append("close"))

    def poll(self):
        return 0

    def terminate(self):
        calls.append("terminate")


_sp.Popen = FakePopen

cfg = vh.Config(mock=True)
hub = vh.Hub(cfg)
card = hub._speaker_card()
print("speaker_card =", card)
assert card.startswith("hw:"), "카드 자동선택 실패"

hub.play_local(b"FAKEMP3" * 10)
time.sleep(0.3)
print("calls:", calls)
assert any(isinstance(c, tuple) and c[0] == "write" for c in calls), "mp3 전송 없음"
hub.stop_local()
print("OK: play_local/stop_local 흐름 통과")

cfg2 = vh.Config(mock=True, speaker="off")
hub2 = vh.Hub(cfg2)
assert hub2._speaker_card() == ""
hub2.play_local(b"x")
assert hub2._speaker_proc is None
print("OK: speaker=off 시 재생 안 함")

cfg3 = vh.Config(mock=True, speaker="hw:1,7")
hub3 = vh.Hub(cfg3)
assert hub3._speaker_card() == "hw:1,7"
print("OK: 카드 직접 지정 통과")
