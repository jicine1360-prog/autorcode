"""음성 허브 테스트 — Room(파일 유지) + aiohttp 허브 흐름(WS·say·stt·tts).

실제 whisper/edge-tts 는 쓰지 않고 주입(mock)으로 배관을 검증한다.
"""
import asyncio
import json
import os
import tempfile
import unittest

try:
    import aiohttp
    from aiohttp.test_utils import TestServer
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

from harness.voicehub import Config, Hub, Room, StreamVAD, make_app


def _sine(ms, freq=440.0, amp=0.4, rate=16000):
    import numpy as np
    n = int(rate * ms / 1000)
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def _silence(ms, rate=16000):
    import numpy as np
    return np.zeros(int(rate * ms / 1000), dtype=np.float32)


def _pcm_bytes(frames):
    import numpy as np
    return (np.concatenate(frames) * 32767).astype(np.int16).tobytes()


class StreamVADTests(unittest.TestCase):
    def _feed(self, vad, frames):
        events = []
        for f in frames:
            events.extend(vad.feed(f))
        return events

    def test_speech_start_and_end_with_preroll(self):
        vad = StreamVAD()
        frames = [_silence(100)] * 10 + [_sine(100)] * 5 + [_silence(100)] * 8
        events = self._feed(vad, frames)
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["speech_start", "speech_end"])
        seg = events[1][1]
        self.assertGreaterEqual(len(seg), 16000 * 0.5)
        self.assertLessEqual(len(seg), 16000 * 2.0)

    def test_short_blip_ignored(self):
        vad = StreamVAD(min_speech_ms=200)
        events = self._feed(vad, [_silence(100)] * 5 + [_sine(100)] + [_silence(100)] * 10)
        self.assertEqual(events, [])

    def test_two_utterances(self):
        vad = StreamVAD()
        events = self._feed(vad, [_silence(100)] * 3 + [_sine(100)] * 4 + [_silence(100)] * 8
                            + [_sine(100)] * 4 + [_silence(100)] * 8)
        self.assertEqual([e[0] for e in events],
                         ["speech_start", "speech_end", "speech_start", "speech_end"])

    def test_reset_clears_state(self):
        vad = StreamVAD()
        self._feed(vad, [_sine(100)] * 4)
        vad.reset()
        self.assertIsNone(vad.seg)
        self.assertEqual(self._feed(vad, [_silence(100)] * 3), [])


class RoomTests(unittest.TestCase):
    def test_add_and_reload_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "room.jsonl")
            room = Room(path)
            room.add("user", "안녕", "웹")
            room.add("assistant", "반가워요", "AI")
            again = Room(path)
            self.assertEqual([e["text"] for e in again.history], ["안녕", "반가워요"])
            self.assertEqual(again.history[1]["role"], "assistant")

    def test_context_maps_roles_and_strips(self):
        room = Room("")
        room.add("user", "  질문  ", "웹")
        room.add("assistant", "답", "AI")
        ctx = room.context("시스템", n=10)
        self.assertEqual(ctx[0]["role"], "system")
        self.assertEqual(ctx[1], {"role": "user", "content": "질문"})
        self.assertEqual(ctx[2]["role"], "assistant")


@unittest.skipUnless(HAS_AIOHTTP, "aiohttp 없음")
class HubFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _hub(self, mock=False):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        tmp.close()
        cfg = Config(room_file=tmp.name, mock=mock, port=0)
        hub = Hub(cfg)
        hub.stt = lambda path: "테스트 발화"

        async def llm(cfg_, messages):
            for piece in ("안녕", "하세요"):
                yield piece

        async def tts(cfg_, text, voice=""):
            return b"ID3FAKE"

        hub.llm = llm
        hub.tts = tts
        server = TestServer(make_app(hub))
        await server.start_server()
        self.addCleanup(server.close)
        self.addCleanup(lambda: os.unlink(tmp.name))
        return hub, server

    async def _recv_until(self, ws, kind, limit=20):
        for _ in range(limit):
            msg = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
            if msg.get("type") == kind:
                return msg
        self.fail(f"이벤트 {kind} 를 못 받음")

    async def test_health_and_state(self):
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            r = await s.get(server.make_url("/health"))
            body = await r.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["history"], 0)
            hub.room.add("user", "미리 있던 말", "웹")
            r = await s.get(server.make_url("/state"))
            body = await r.json()
            self.assertEqual(body["history"][-1]["text"], "미리 있던 말")

    async def test_say_broadcasts_user_then_ai_with_audio(self):
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                snap = json.loads(await ws.receive_str())
                self.assertEqual(snap["type"], "state")
                r = await s.post(server.make_url("/say"), json={"text": "반가워", "who": "터미널"})
                self.assertTrue((await r.json())["ok"])
                user = await self._recv_until(ws, "user")
                self.assertEqual((user["text"], user["who"]), ("반가워", "터미널"))
                deltas = [await self._recv_until(ws, "ai_delta") for _ in range(2)]
                self.assertEqual("".join(d["text"] for d in deltas), "안녕하세요")
                ai = await self._recv_until(ws, "ai")
                self.assertEqual(ai["text"], "안녕하세요")
                self.assertTrue(ai["audio"].startswith("/tts/"))
                audio = await s.get(server.make_url(ai["audio"]))
                self.assertEqual(audio.status, 200)
                self.assertEqual(audio.headers["Content-Type"], "audio/mpeg")
                self.assertEqual(await audio.read(), b"ID3FAKE")

    async def test_ws_client_can_say(self):
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                await ws.receive_str()
                await ws.send_str(json.dumps({"type": "say", "text": "웹에서 보냄", "who": "웹"}))
                user = await self._recv_until(ws, "user")
                self.assertEqual(user["text"], "웹에서 보냄")
                ai = await self._recv_until(ws, "ai")
                self.assertEqual(ai["text"], "안녕하세요")

    async def test_empty_say_rejected(self):
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            r = await s.post(server.make_url("/say"), json={"text": "   "})
            self.assertEqual(r.status, 400)

    async def test_stt_mock_mode_broadcasts(self):
        hub, server = await self._hub(mock=True)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                await ws.receive_str()
                r = await s.post(server.make_url("/stt?who=폰"), data=b"\x1aE\xdf\xa3fake",
                                 headers={"Content-Type": "audio/webm"})
                body = await r.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["text"], "목소리 테스트입니다")
                user = await self._recv_until(ws, "user")
                self.assertEqual(user["who"], "폰")
                ai = await self._recv_until(ws, "ai")
                self.assertTrue(ai["text"])

    async def test_empty_stt_rejected(self):
        hub, server = await self._hub(mock=True)
        async with aiohttp.ClientSession() as s:
            r = await s.post(server.make_url("/stt"), data=b"",
                             headers={"Content-Type": "audio/webm"})
            self.assertEqual(r.status, 400)

    async def test_index_page_served(self):
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            r = await s.get(server.make_url("/"))
            html = await r.text()
            self.assertIn("음성방", html)
            self.assertIn("getUserMedia", html)
            self.assertIn("/ws", html)

    async def test_binary_audio_stream_transcribes_and_replies(self):
        """통화 모드 — PCM 조각을 계속 보내면 VAD→전사→답변이 자동으로 흐른다."""
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                await ws.receive_str()
                await ws.send_str(json.dumps({"type": "who", "who": "폰"}))
                pcm = _pcm_bytes([_silence(100)] * 5 + [_sine(100)] * 5 + [_silence(100)] * 8)
                for off in range(0, len(pcm), 3200):
                    await ws.send_bytes(pcm[off:off + 3200])
                user = await self._recv_until(ws, "user")
                self.assertEqual((user["text"], user["who"]), ("테스트 발화", "폰"))
                ai = await self._recv_until(ws, "ai")
                self.assertEqual(ai["text"], "안녕하세요")

    async def test_barge_in_cancels_reply(self):
        """AI 말하는 중에 말소리가 들리면 interrupt 방송 + 생성 취소(부분 기록)."""
        hub, server = await self._hub()

        async def slow_llm(cfg_, messages):
            for piece in ("첫", "번", "째"):
                await asyncio.sleep(0.15)
                yield piece
            yield "끝"  # 여기까지 오면 끼어들기 실패

        hub.llm = slow_llm
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                await ws.receive_str()
                await ws.send_str(json.dumps({"type": "say", "text": "시작", "who": "터미널"}))
                await self._recv_until(ws, "user")
                await self._recv_until(ws, "ai_delta")
                pcm = _pcm_bytes([_silence(100)] * 3 + [_sine(100)] * 4)
                for off in range(0, len(pcm), 3200):
                    await ws.send_bytes(pcm[off:off + 3200])
                found = False
                for _ in range(10):
                    msg = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                    if msg.get("type") == "interrupt":
                        found = True
                        break
                self.assertTrue(found, "interrupt 이벤트가 없음")
                await asyncio.sleep(0.6)
                self.assertFalse(
                    any(e.get("text") == "첫번째끝" for e in hub.room.history
                        if e.get("role") == "assistant"),
                    "끼어들기 후에도 답변이 끝까지 생성됨")

    async def test_duet_alternates_turns_and_ends(self):
        """듀엣 — 로이와 봇이 번갈아 말하고, 각자 목소리로 오디오가 붙고, 끝난다."""
        hub, server = await self._hub()
        voices = []

        async def tts_capture(cfg_, text, voice=""):
            voices.append(voice)
            return b"ID3FAKE"

        hub.tts = tts_capture

        async def numbered_llm(cfg_, messages):
            n = sum(1 for e in hub.room.history) + 1
            await asyncio.sleep(0.02)
            yield f"발언{n}"

        hub.llm = numbered_llm
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                await ws.receive_str()
                await ws.send_str(json.dumps({"type": "duet", "topic": "점심 메뉴",
                                              "turns": 2}))
                speakers, audios, started, ended = [], [], False, False
                for _ in range(30):
                    msg = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                    if msg.get("type") == "duet" and msg.get("state") == "start":
                        started = True
                    if msg.get("type") == "duet" and msg.get("state") == "end":
                        ended = True
                        break
                    if msg.get("type") in ("ai", "bot") and msg.get("duet"):
                        speakers.append(msg["who"])
                        audios.append(msg.get("audio", ""))
                self.assertTrue(started and ended)
                self.assertEqual(speakers, ["로이", "건우", "로이", "건우"])
                self.assertTrue(all(a.startswith("/tts/") for a in audios))
                self.assertIn("ko-KR-InJoonNeural", voices)
                roles = [e["role"] for e in hub.room.history[-4:]]
                self.assertEqual(roles, ["assistant", "user", "assistant", "user"])
                self.assertEqual(len(hub.audio), 4)

    async def test_duet_stops_when_human_speaks(self):
        """듀엣 중 사람이 말을 걸면(글자) 봇 대화는 멈추고 답한다."""
        hub, server = await self._hub()

        async def slow_llm(cfg_, messages):
            for i in range(3):
                await asyncio.sleep(0.15)
                yield f"조각{i}"
            yield "끝"  # human 끼어들기 실패 감지용

        hub.llm = slow_llm
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.make_url("/ws")) as ws:
                await ws.receive_str()
                await ws.send_str(json.dumps({"type": "duet", "topic": "테스트", "turns": 20}))
                await self._recv_until(ws, "duet_turn")
                await s.post(server.make_url("/say"), json={"text": "잠깐만", "who": "터미널"})
                ended = False
                for _ in range(40):
                    msg = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                    if msg.get("type") == "duet" and msg.get("state") == "end":
                        ended = True
                        break
                self.assertTrue(ended, "듀엣이 중단되지 않음")
                await asyncio.sleep(0.5)
                self.assertFalse(
                    any(e.get("who") == "건우" for e in hub.room.history),
                    "중단됐는데도 봇이 발언을 마침")

    async def test_duet_rejects_empty_topic(self):
        hub, server = await self._hub()
        async with aiohttp.ClientSession() as s:
            r = await s.post(server.make_url("/duet"), json={"topic": "  "})
            self.assertEqual(r.status, 400)
            body = await r.json()
            self.assertFalse(body["ok"])
