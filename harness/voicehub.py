"""음성 대화 허브 — 브라우저(폰/PC) 마이크 ↔ AI ↔ TUI 자막을 한 방으로 엮는다.

한 방(Room)에서 사용자와 AI 가 주고받은 모든 말이 공유되고, 어느 클라이언트로
들어와도 같은 대화에 참여한다:
  - 브라우저 페이지: 마이크(PTT) 로 말하면 whisper STT → AI → edge-tts 음성 재생
  - 통화 모드(풀 듀플렉스): 마이크를 0.1초 조각으로 계속 보내면 서버 VAD 가
    말 시작/끝을 판정 — 말이 끝나면 자동 전사·투입, AI 가 말하는 중에 말소리가
    들리면 끼어들기(interrupt): LLM 생성 취소 + 모든 브라우저 재생 즉시 정지
  - TUI(`/voice`): 실시간 자막으로 보고, 타이핑한 글도 방에 투입(다른 곳에서 소리로 읽힘)
  - 봇끼리 대화(듀엣): 주제를 주면 '로이'(방 AI)와 봇 1:1이 번갈아 자동 대화 —
    각자 다른 목소리로 브라우저에 들리고, 사람이 말하면 양보한다

HTTP:
  GET  /            폰 친화 음성 페이지
  POST /stt?who=..  오디오(webm/mp4/wav...) → 글자(whisper) → 방에 투입
  POST /say         {"text":..,"who":..} 글자 투입(터미널·curl 등)
  POST /duet        {"topic":..,"bot":{"name","system","voice"},"turns":N} 시작 / {"stop":true} 중단
  GET  /state       현재 방 기록 JSON
  GET  /tts/<id>.mp3 AI 답변 음성(edge-tts)
  GET  /health
WS /ws:
  서버→클라  {"type":"state","history":[...]}
             {"type":"user","who":..,"text":..,"ts":..}
             {"type":"ai_delta","text":"조각"}
             {"type":"ai","text":"전체","audio":"/tts/xx.mp3"}
             {"type":"bot_delta","who":"건우","text":"조각"}
             {"type":"bot","who":"건우","text":"전체","audio":"/tts/xx.mp3"}
             {"type":"duet","state":"start|end","topic":..,"bot":..}
             {"type":"interrupt"}                 # AI 말 중 끼어들기 — 재생 정지
             {"type":"vad","speaking":true|false,"who":..}
             {"type":"status","text":"..."}
  클라→서버  {"type":"say","text":"..","who":"터미널"}
             {"type":"who","who":"폰"}            # 이 접속의 이름
             {"type":"duet","topic":"주제"}       # 봇 대화 시작
             {"type":"duet","stop":true}          # 봇 대화 중단
             {"type":"call","on":false}           # 통화 종료(버퍼 리셋)
             (바이너리) 16kHz·모노·int16 PCM 조각 → VAD/전사

환경변수(기본값):
  VOICEHUB_PORT=8096   VOICEHUB_HOST=0.0.0.0
  VOICEHUB_BASE_URL=http://127.0.0.1:8095/v1   VOICEHUB_MODEL=qwen3.8-flash-next-iq3_s
  VOICEHUB_API_KEY_FILE=~/.strata/api.key      VOICEHUB_API_KEY=(직접 지정 우선)
  VOICEHUB_STT_MODEL=small   VOICEHUB_STT_DEVICE=auto   VOICEHUB_STT_LANG=ko
  VOICEHUB_VAD_THRESHOLD=0.010  VOICEHUB_VAD_SILENCE_MS=600  VOICEHUB_VAD_MIN_SPEECH_MS=200
  VOICEHUB_TTS_VOICE=ko-KR-SunHiNeural   VOICEHUB_TTS_BIN=(자동탐색)
  VOICEHUB_ROOM=~/.autorcode/voice_room.jsonl          VOICEHUB_SYSTEM=(시스템 프롬프트)
  VOICEHUB_TLS_CERT / VOICEHUB_TLS_KEY (있으면 HTTPS — 마이크는 HTTPS/localhost 필요)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections import OrderedDict
from pathlib import Path

try:  # numpy 는 선택 가속 — 없어도 표준 array 로 같은 계산을 한다 (autorcode 무설치 원칙)
    import numpy as np
except ImportError:
    np = None

import array as _array
import math as _math


def _f32_from_pcm(pcm: bytes):
    """int16 PCM 바이트 → float32 프레임 시퀀스 ([-1,1))."""
    if np is not None:
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    a = _array.array("h")
    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return [v / 32768.0 for v in a]


def _f32_concat(frames) -> list:
    out: list = []
    for f in frames:
        out.extend(f)
    return out


def _f32_to_pcm16(seg) -> bytes:
    if np is not None:
        return (np.clip(seg, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    a = _array.array("h", (max(-1.0, min(1.0, v)) * 32767 for v in seg))
    return a.tobytes()


def _rms(frame) -> float:
    if not len(frame):
        return 0.0
    if np is not None:
        return float(np.sqrt(np.mean(np.asarray(frame, dtype=np.float32) ** 2)))
    return _math.sqrt(sum(v * v for v in frame) / len(frame))
try:  # aiohttp 는 허브 서버(web)에만 필요 — 없어도 Config/Room/StreamVAD 는 동작 (무설치 원칙)
    from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

DEFAULT_SYSTEM = (
    "너는 사용자와 음성으로 대화하는 친근한 파트너 '스트라타'다. "
    "말하듯 자연스러운 구어체로 2~4문장 안으로 짧게 답한다. "
    "목록·표·마크다운·이모지는 쓰지 마라(소리내어 읽기 때문). "
    "숫자·고유명사는 또박또박, 필요하면 문장으로 풀어 말한다. "
    "모르면 솔직히 말하고 되물어 확인한다."
)

DUET_BOT = {
    "name": "건우",
    "voice": "ko-KR-InJoonNeural",
    "system": ("너는 '건우'다. 호기심 많고 가끔 토를 다는 성격으로, 친구 '로이'와 "
               "주어진 주제로 가볍게 티키타카 대화를 한다. 말하듯 1~2문장으로만 말하고, "
               "상대 말을 받아치거나 질문을 던진다. 목록·표·마크다운 금지."),
}
DUET_LOI_ADD = (" 지금은 '{bot}'와(과) 주제 '{topic}'로 대화하는 중이다. "
                "말하듯 1~2문장으로만 답하고, 상대에게 받아치거나 되물어라. 목록·마크다운 금지.")


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


class Config:
    def __init__(self, **over):
        env = os.environ.get
        self.host = over.get("host") or env("VOICEHUB_HOST", "0.0.0.0")
        self.port = int(over.get("port") or env("VOICEHUB_PORT", "8096"))
        self.base_url = (over.get("base_url") or env("VOICEHUB_BASE_URL",
                        "http://127.0.0.1:8095/v1")).rstrip("/")
        self.model = over.get("model") or env("VOICEHUB_MODEL", "qwen3.8-flash-next-iq3_s")
        self.api_key = over.get("api_key") or env("VOICEHUB_API_KEY", "").strip()
        if not self.api_key:
            key_file = env("VOICEHUB_API_KEY_FILE", _home(".strata", "api.key"))
            try:
                self.api_key = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError:
                self.api_key = ""
        self.stt_model = over.get("stt_model") or env("VOICEHUB_STT_MODEL", "small")
        self.stt_device = over.get("stt_device") or env("VOICEHUB_STT_DEVICE", "auto")
        self.stt_lang = over.get("stt_lang") or env("VOICEHUB_STT_LANG", "ko")
        self.vad_threshold = float(over.get("vad_threshold") or env("VOICEHUB_VAD_THRESHOLD", "0.010"))
        self.vad_silence_ms = int(over.get("vad_silence_ms") or env("VOICEHUB_VAD_SILENCE_MS", "600"))
        self.vad_min_speech_ms = int(over.get("vad_min_speech_ms") or env("VOICEHUB_VAD_MIN_SPEECH_MS", "200"))
        self.tts_voice = over.get("tts_voice") or env("VOICEHUB_TTS_VOICE", "ko-KR-SunHiNeural")
        self.tts_bin = over.get("tts_bin") or env("VOICEHUB_TTS_BIN", "") or self._find_tts()
        self.room_file = over.get("room_file") or env("VOICEHUB_ROOM", _home(".autorcode", "voice_room.jsonl"))
        self.system = over.get("system") or env("VOICEHUB_SYSTEM", DEFAULT_SYSTEM)
        self.tls_cert = over.get("tls_cert") or env("VOICEHUB_TLS_CERT", _home(".autorcode", "voice_cert.pem"))
        self.tls_key = over.get("tls_key") or env("VOICEHUB_TLS_KEY", _home(".autorcode", "voice_key.pem"))
        self.ffmpeg = over.get("ffmpeg") or env("VOICEHUB_FFMPEG", "") or (shutil.which("ffmpeg") or "ffmpeg")
        self.mock = bool(over.get("mock", False))

    @staticmethod
    def _find_tts() -> str:
        cands = [shutil.which("edge-tts"),
                 _home(".hermes", "hermes-agent", "venv", "bin", "edge-tts"),
                 _home(".local", "bin", "edge-tts")]
        for c in cands:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return ""

    def scheme(self) -> str:
        if self.tls_cert and self.tls_key and os.path.isfile(self.tls_cert) and os.path.isfile(self.tls_key):
            return "https"
        return "http"


class Room:
    """방 대화 기록 — 메모리 + JSONL 로 견고하게 유지."""

    def __init__(self, path: str = ""):
        self.path = path
        self.history: list[dict] = []
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            self.history.append(json.loads(line))
                self.history = self.history[-400:]
            except OSError:
                pass
            except ValueError:
                pass

    def add(self, role: str, text: str, who: str = "") -> dict:
        entry = {"role": role, "who": who, "text": text, "ts": time.time()}
        self.history.append(entry)
        if self.path:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return entry

    def context(self, system: str, n: int = 30) -> list[dict]:
        msgs = [{"role": "system", "content": system}]
        for e in self.history[-n:]:
            role = "assistant" if e.get("role") == "assistant" else "user"
            text = str(e.get("text", "")).strip()
            if text:
                msgs.append({"role": role, "content": text})
        return msgs


_WHISPER = {"model": None, "key": None, "lock": threading.Lock()}


class StreamVAD:
    """실시간 말소리 감지 — 0.1초 조각을 먹여 speech_start/speech_end 를 뱉는다.

    끝날 때 구간(프리롤 포함, float32 [-1,1])을 함께 돌려준다. 순수 파이썬이라
    테스트에서 그대로 쓸 수 있다. energy 기반(브라우저 AEC·노이즈억제 전제).
    """

    FRAME = 1600  # 100ms @16k

    def __init__(self, threshold=0.010, min_speech_ms=200, silence_ms=600,
                 preroll_ms=400, max_segment_s=30):
        self.threshold = float(threshold)
        self.min_speech = max(1, int(min_speech_ms) // 100)
        self.silence_need = max(1, int(silence_ms) // 100)
        self.preroll_need = max(1, int(preroll_ms) // 100)
        self.max_frames = max(1, int(max_segment_s) * 10)
        self.reset()

    def reset(self):
        self.preroll: list = []
        self.seg: list | None = None
        self.speech_run = 0
        self.silence_run = 0
        self.floor = 0.004

    def feed(self, frame):
        rms = _rms(frame)
        speaking = rms > max(self.threshold, self.floor * 3.0)
        events: list[tuple[str, object]] = []
        if self.seg is None:
            self.preroll.append(frame)
            if len(self.preroll) > self.preroll_need:
                self.preroll.pop(0)
            if speaking:
                self.speech_run += 1
                if self.speech_run >= self.min_speech:
                    self.seg = list(self.preroll)
                    self.silence_run = 0
                    events.append(("speech_start", None))
            else:
                self.speech_run = 0
                self.floor = self.floor * 0.995 + rms * 0.005
        else:
            self.seg.append(frame)
            if speaking:
                self.silence_run = 0
            else:
                self.silence_run += 1
            if self.silence_run >= self.silence_need or len(self.seg) >= self.max_frames:
                seg = _f32_concat(self.seg)
                self.reset()
                events.append(("speech_end", seg))
        return events


class Session:
    """웹소켓 한 접속의 통화 상태 — 이름·VAD·들어온 바이트 조각 버퍼."""

    FRAME_BYTES = StreamVAD.FRAME * 2  # int16

    def __init__(self, ws, cfg: "Config | None" = None):
        self.ws = ws
        self.who = "웹"
        self.leftover = b""
        self.vad = StreamVAD(
            threshold=cfg.vad_threshold if cfg else 0.010,
            min_speech_ms=cfg.vad_min_speech_ms if cfg else 200,
            silence_ms=cfg.vad_silence_ms if cfg else 600)

    def reset(self):
        self.leftover = b""
        self.vad.reset()


def load_whisper(name: str, device: str):
    """faster-whisper 모델을 전역 캐시로 로드(스레드 안전). 실패는 예외로."""
    from faster_whisper import WhisperModel
    key = (name, device)
    with _WHISPER["lock"]:
        if _WHISPER["model"] is not None and _WHISPER["key"] == key:
            return _WHISPER["model"]

        def _load(dev):
            if dev == "auto":
                dev = "cuda"
            compute = "float16" if str(dev).startswith("cuda") else "int8"
            return WhisperModel(name, device=dev, compute_type=compute)

        try:
            model = _load(device)
        except Exception:
            if device == "auto":
                model = _load("cpu")
            else:
                raise
        _WHISPER["model"], _WHISPER["key"] = model, (name, device)
        return model


def transcribe_file(path: str, cfg: Config) -> str:
    """wav 파일 → 글자. 실패하면 빈 문자열."""
    try:
        model = load_whisper(cfg.stt_model, cfg.stt_device)
        try:
            segments, _info = model.transcribe(path, language=cfg.stt_lang or None,
                                               vad_filter=True, beam_size=1)
            text = " ".join(s.text.strip() for s in segments)
        except Exception:
            segments, _info = model.transcribe(path, language=cfg.stt_lang or None, beam_size=1)
            text = " ".join(s.text.strip() for s in segments)
        return text.strip()
    except Exception:
        return ""


async def to_wav(cfg: Config, data: bytes, suffix: str) -> str:
    """브라우저 오디오(webm/mp4/ogg...) → 16k 모노 wav 임시파일. 경로 반환."""
    src = tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".bin")
    src.write(data)
    src.close()
    dst = src.name + ".wav"
    proc = await asyncio.create_subprocess_exec(
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", src.name,
        "-ar", "16000", "-ac", "1", "-f", "wav", dst, "-y",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    code = await proc.wait()
    try:
        os.unlink(src.name)
    except OSError:
        pass
    if code != 0:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise RuntimeError("ffmpeg 변환 실패 — 오디오 형식을 알아볼 수 없습니다")
    return dst


async def default_llm_stream(cfg: Config, messages: list[dict]):
    """Strata(OpenAI 호환) 스트리밍 — 조각(str)을 yield."""
    payload = {"model": cfg.model, "messages": messages, "stream": True,
               "temperature": 0.6, "max_tokens": 400}
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    timeout = ClientTimeout(total=180, sock_read=120)
    async with ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(f"{cfg.base_url}/chat/completions", json=payload) as r:
            if r.status != 200:
                body = (await r.text())[:200]
                raise RuntimeError(f"모델 오류 {r.status}: {body}")
            async for raw in r.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                for ch in obj.get("choices") or []:
                    piece = ((ch.get("delta") or {}).get("content")) or ""
                    if piece:
                        yield piece


async def mock_llm(cfg: Config, messages: list[dict]):
    """테스트용 가짜 답변 — 마지막 사용자 말을 되받는다."""
    await asyncio.sleep(0.05)
    last = next((str(m.get("content", "")) for m in reversed(messages)
                 if m.get("role") == "user"), "")
    for piece in ("(가짜 응답) ", f"{last[:40]} 라고 하셨죠?"):
        await asyncio.sleep(0.02)
        yield piece


async def default_tts(cfg: Config, text: str, voice: str = "") -> bytes:
    """edge-tts 로 mp3 바이트 생성 — voice 미지정이면 cfg.tts_voice. 실패면 빈 바이트."""
    if not cfg.tts_bin or not text:
        return b""
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    out.close()
    try:
        proc = await asyncio.create_subprocess_exec(
            cfg.tts_bin, "--voice", voice or cfg.tts_voice, "--text", text[:1200],
            "--write-media", out.name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=45)
        except asyncio.TimeoutError:
            proc.kill()
            return b""
        if code != 0:
            return b""
        with open(out.name, "rb") as fh:
            return fh.read()
    except OSError:
        return b""
    finally:
        try:
            os.unlink(out.name)
        except OSError:
            pass


class Hub:
    """허브 본체 — 테스트에서는 stt/llm/tts 를 갈아끼울 수 있다."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.room = Room(self.cfg.room_file)
        self.clients: set[web.WebSocketResponse] = set()
        self.sessions: dict[object, Session] = {}
        self.audio: OrderedDict[str, bytes] = OrderedDict()
        self._busy = asyncio.Lock()
        self._respond_task: asyncio.Task | None = None
        self._duet_task: asyncio.Task | None = None
        self.frames_in = 0
        self.stt = self._stt_default if not self.cfg.mock else (lambda path: "목소리 테스트입니다")
        self.llm = mock_llm if self.cfg.mock else default_llm_stream
        self.tts = default_tts
        self._mock_silence: bytes | None = None

    # ---- STT ----
    def _stt_default(self, path: str) -> str:
        return transcribe_file(path, self.cfg)

    async def _stt_audio(self, data: bytes, suffix: str) -> str:
        if self.cfg.mock:
            if data:
                try:
                    os.unlink(await to_wav(self.cfg, data, suffix))
                except Exception:
                    pass
            return "목소리 테스트입니다"
        try:
            wav = await to_wav(self.cfg, data, suffix)
        except Exception:
            return ""
        try:
            return await asyncio.get_running_loop().run_in_executor(None, self.stt, wav)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass

    # ---- 방 ----
    async def broadcast(self, obj: dict):
        dead = []
        data = json.dumps(obj, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def say(self, text: str, who: str = "웹"):
        text = (text or "").strip()
        if not text:
            return
        if self._duet_task and not self._duet_task.done():
            self.stop_duet()  # 사람이 말을 걸면 봇 대화는 양보
        entry = self.room.add("user", text, who)
        await self.broadcast({"type": "user", **entry})
        self._respond_task = asyncio.create_task(self.respond())

    def cancel_reply(self):
        """끼어들기 — 진행 중 AI 답변 생성을 취소(부분 답변은 respond 안에서 정리)."""
        task = self._respond_task
        if task and not task.done():
            task.cancel()

    # ---- 봇끼리 대화 (듀엣) — 주제를 주면 로이와 봇이 번갈아 자동 대화 ----
    def start_duet(self, topic: str, bot: dict | None = None, turns: int = 10):
        topic = (topic or "").strip()
        if not topic:
            return False, "주제가 필요합니다"
        if self._duet_task and not self._duet_task.done():
            return False, "이미 봇 대화가 진행 중입니다"
        b = dict(DUET_BOT)
        for k, v in (bot or {}).items():
            if k in b and v:
                b[k] = str(v)
        self._duet_task = asyncio.create_task(
            self.run_duet(topic, b, max(2, min(int(turns), 40))))
        return True, ""

    def stop_duet(self):
        task = self._duet_task
        if task and not task.done():
            task.cancel()

    def _duet_messages(self, topic: str, bot: dict, transcript: list, speaker: str):
        if speaker == "로이":
            system = self.cfg.system + DUET_LOI_ADD.format(bot=bot["name"], topic=topic)
        else:
            system = bot["system"]
        msgs = [{"role": "system", "content": system}]
        for name, text in transcript:
            role = "assistant" if name == speaker else "user"
            msgs.append({"role": role, "content": text})
        msgs.append({"role": "user",
                     "content": f"주제: {topic}\n이제 네 차례다. 1~2문장으로만 말하라."})
        return msgs

    async def run_duet(self, topic: str, bot: dict, turns: int):
        await self.broadcast({"type": "duet", "state": "start", "topic": topic, "bot": bot["name"]})
        transcript: list[tuple[str, str]] = []
        try:
            async with self._busy:
                for _ in range(turns):
                    for speaker in ("로이", bot["name"]):
                        msgs = self._duet_messages(topic, bot, transcript, speaker)
                        await self.broadcast({"type": "duet_turn", "who": speaker})
                        parts: list[str] = []
                        async for piece in self.llm(self.cfg, msgs):
                            parts.append(piece)
                            await self.broadcast({"type": "bot_delta", "who": speaker, "text": piece})
                        text = "".join(parts).strip()
                        if not text:
                            continue
                        voice = bot["voice"] if speaker == bot["name"] else ""
                        audio_url = ""
                        try:
                            mp3 = await self._tts_bytes(text, voice)
                            if mp3:
                                aid = uuid.uuid4().hex[:10]
                                self.audio[aid] = mp3
                                while len(self.audio) > 40:
                                    self.audio.popitem(last=False)
                                audio_url = f"/tts/{aid}.mp3"
                        except Exception:
                            pass
                        role = "assistant" if speaker == "로이" else "user"
                        entry = self.room.add(role, text, speaker)
                        await self.broadcast({"type": "ai" if speaker == "로이" else "bot",
                                              "who": speaker, "text": text, "audio": audio_url,
                                              "ts": entry["ts"], "duet": True})
                        transcript.append((speaker, text))
                        await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self.broadcast({"type": "duet", "state": "end", "topic": topic})
            except Exception:
                pass

    async def respond(self):
        """AI 답변 — 스트리밍 조각을 방송하고, 끝나면 음성(mp3)까지 준비해 방송."""
        async with self._busy:
            await self.broadcast({"type": "status", "text": "생각 중…"})
            parts: list[str] = []
            try:
                async for piece in self.llm(self.cfg, self.room.context(self.cfg.system)):
                    parts.append(piece)
                    await self.broadcast({"type": "ai_delta", "text": piece})
            except asyncio.CancelledError:
                await self._finish_interrupted(parts)
                return
            except Exception as e:
                parts.append(f"죄송해요, 방금 답을 만들다 문제가 생겼어요: {e}")
            full = "".join(parts).strip() or "죄송해요, 답을 못 만들었어요."
            entry = self.room.add("assistant", full, "AI")
            audio_url = ""
            try:
                mp3 = await self._tts_bytes(full)
                if mp3:
                    aid = uuid.uuid4().hex[:10]
                    self.audio[aid] = mp3
                    while len(self.audio) > 40:
                        self.audio.popitem(last=False)
                    audio_url = f"/tts/{aid}.mp3"
            except asyncio.CancelledError:
                await self._finish_interrupted(parts)
                return
            except Exception:
                pass
            await self.broadcast({"type": "ai", "text": full, "audio": audio_url, "ts": entry["ts"]})

    async def _finish_interrupted(self, parts: list[str]):
        """끼어들기 정리 — 재생 정지 방송 + 이미 말한 부분은 기록에 남긴다."""
        try:
            await self.broadcast({"type": "interrupt"})
        except Exception:
            pass
        partial = "".join(parts).strip()
        if len(partial) >= 8:
            entry = self.room.add("assistant", partial + " …", "AI")
            try:
                await self.broadcast({"type": "ai", "text": partial + " …", "audio": "",
                                      "interrupted": True, "ts": entry["ts"]})
            except Exception:
                pass

    # ---- 풀 듀플렉스(통화) — 바이너리 오디오 조각 ----
    async def feed_audio(self, session: Session, data: bytes):
        buf = session.leftover + data
        n = len(buf) // Session.FRAME_BYTES
        if n == 0:
            session.leftover = buf
            return
        usable = buf[:n * Session.FRAME_BYTES]
        session.leftover = buf[n * Session.FRAME_BYTES:]
        self.frames_in += n
        arr = _f32_from_pcm(usable)
        for i in range(n):
            frame = arr[i * StreamVAD.FRAME:(i + 1) * StreamVAD.FRAME]
            for ev, seg in session.vad.feed(frame):
                if ev == "speech_start":
                    await self._on_speech_start(session)
                else:
                    await self.broadcast({"type": "vad", "speaking": False, "who": session.who})
                    asyncio.create_task(self._transcribe_and_say(session, seg))

    async def _on_speech_start(self, session: Session):
        await self.broadcast({"type": "vad", "speaking": True, "who": session.who})
        if self._duet_task and not self._duet_task.done():
            self.stop_duet()
            return
        if self._respond_task and not self._respond_task.done():
            self.cancel_reply()

    async def _transcribe_and_say(self, session: Session, seg):
        if seg is None or len(seg) < StreamVAD.FRAME:
            return
        pcm = _f32_to_pcm16(seg)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
            text = await asyncio.get_running_loop().run_in_executor(None, self.stt, tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        if text and text.strip():
            await self.say(text, session.who)

    async def _tts_bytes(self, text: str, voice: str = "") -> bytes:
        if not self.cfg.mock:
            return await self.tts(self.cfg, text, voice)
        if self._mock_silence is None:
            out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            out.close()
            proc = await asyncio.create_subprocess_exec(
                self.cfg.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.3",
                out.name, "-y",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            try:
                with open(out.name, "rb") as fh:
                    self._mock_silence = fh.read()
            except OSError:
                self._mock_silence = b""
            try:
                os.unlink(out.name)
            except OSError:
                pass
        return self._mock_silence

    # ---- HTTP 핸들러 ----
    async def page(self, _req: web.Request) -> web.Response:
        return web.FileResponse(PAGE_FILE)

    async def health(self, _req: web.Request) -> web.Response:
        return web.json_response({
            "ok": True, "model": self.cfg.model, "mock": self.cfg.mock,
            "tts": bool(self.cfg.tts_bin) or self.cfg.mock, "stt": self.cfg.stt_model,
            "duplex": True, "vad": self.cfg.vad_threshold, "frames": self.frames_in,
            "duet": bool(self._duet_task and not self._duet_task.done()),
            "history": len(self.room.history), "clients": len(self.clients)})

    async def state(self, _req: web.Request) -> web.Response:
        return web.json_response({"history": self.room.history[-200:]})

    async def say_http(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            body = {}
        text = str(body.get("text", ""))
        who = str(body.get("who", "웹")) or "웹"
        if not text.strip():
            return web.json_response({"ok": False, "error": "빈 텍스트"}, status=400)
        await self.say(text, who)
        return web.json_response({"ok": True})

    async def stt_http(self, req: web.Request) -> web.Response:
        data = await req.read()
        if not data:
            return web.json_response({"ok": False, "text": ""}, status=400)
        who = req.query.get("who", "음성")
        ctype = (req.content_type or "").lower()
        suffix = {"audio/webm": ".webm", "audio/ogg": ".ogg", "video/webm": ".webm",
                  "audio/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/wav": ".wav",
                  "audio/x-wav": ".wav", "audio/aac": ".aac"}.get(ctype, ".webm")
        text = await self._stt_audio(data, suffix)
        if text.strip():
            await self.say(text, who)
        return web.json_response({"ok": bool(text.strip()), "text": text})

    async def duet_http(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("stop"):
            self.stop_duet()
            return web.json_response({"ok": True, "stopped": True})
        ok, err = self.start_duet(str(body.get("topic", "")), body.get("bot"),
                                  int(body.get("turns", 10)))
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        return web.json_response({"ok": True})

    async def tts_http(self, req: web.Request) -> web.Response:
        aid = req.match_info["aid"].replace(".mp3", "")
        mp3 = self.audio.get(aid)
        if not mp3:
            raise web.HTTPNotFound()
        return web.Response(body=mp3, content_type="audio/mpeg",
                            headers={"Cache-Control": "max-age=3600"})

    async def ws_handler(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(req)
        session = Session(ws, self.cfg)
        self.clients.add(ws)
        self.sessions[ws] = session
        try:
            await ws.send_str(json.dumps({"type": "state", "history": self.room.history[-100:]},
                                         ensure_ascii=False))
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self.feed_audio(session, msg.data)
                    continue
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                        break
                    continue
                try:
                    obj = json.loads(msg.data)
                except ValueError:
                    continue
                kind = obj.get("type")
                if kind == "say":
                    await self.say(str(obj.get("text", "")),
                                   str(obj.get("who", "") or session.who) or "웹")
                elif kind == "who":
                    session.who = str(obj.get("who", "") or "웹")[:20]
                elif kind == "duet":
                    if obj.get("stop"):
                        self.stop_duet()
                    else:
                        ok, err = self.start_duet(str(obj.get("topic", "")), obj.get("bot"),
                                                  int(obj.get("turns", 10)))
                        if not ok:
                            await ws.send_str(json.dumps({"type": "status", "text": err},
                                                         ensure_ascii=False))
                elif kind == "call" and not obj.get("on", True):
                    session.reset()
        finally:
            self.clients.discard(ws)
            self.sessions.pop(ws, None)
        return ws


PAGE_FILE = Path(__file__).with_name("voice_page.html")


def make_app(hub: Hub) -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/", hub.page)
    app.router.add_get("/health", hub.health)
    app.router.add_get("/state", hub.state)
    app.router.add_post("/say", hub.say_http)
    app.router.add_post("/duet", hub.duet_http)
    app.router.add_post("/stt", hub.stt_http)
    app.router.add_get("/tts/{aid}", hub.tts_http)
    app.router.add_get("/ws", hub.ws_handler)
    return app


class VoiceRoomClient:
    """TUI 등 동기 코드에서 방에 붙는 클라이언트(웹소켓, 자동 재접속)."""

    def __init__(self, url: str, who: str = "터미널", on_event=None):
        self.url = url
        self.who = who
        self.on_event = on_event or (lambda ev: None)
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="voice-room")
        self._thread.start()

    def _emit(self, obj: dict):
        try:
            self.on_event(obj)
        except Exception:
            pass

    def _run(self):
        import ssl as _ssl
        from websockets.sync.client import connect
        delay = 0.5
        while not self._stop.is_set():
            try:
                ctx = None
                if self.url.startswith("wss://"):
                    ctx = _ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = _ssl.CERT_NONE
                with connect(self.url, ssl=ctx, open_timeout=5,
                             max_size=8 * 1024 * 1024) as ws:
                    with self._lock:
                        self._ws = ws
                    delay = 0.5
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        try:
                            self._emit(json.loads(raw))
                        except ValueError:
                            continue
            except Exception:
                self._emit({"type": "status", "text": "음성방 연결 끊김 — 재시도 중"})
                self._stop.wait(delay)
                delay = min(delay * 2, 8.0)
            finally:
                with self._lock:
                    self._ws = None

    def send(self, text: str) -> bool:
        return self.send_raw({"type": "say", "text": text, "who": self.who})

    def send_raw(self, obj: dict) -> bool:
        with self._lock:
            ws = self._ws
        if ws is None:
            return False
        try:
            ws.send(json.dumps(obj, ensure_ascii=False))
            return True
        except Exception:
            return False

    def stop(self):
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._thread = None


def local_urls(cfg: Config) -> list[str]:
    urls = [f"{cfg.scheme()}://127.0.0.1:{cfg.port}/"]
    try:
        import subprocess
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        for ip in out.split():
            if ip and not ip.startswith(("127.", "172.1", "172.2", "fd", "::")):
                urls.append(f"{cfg.scheme()}://{ip}:{cfg.port}/")
    except Exception:
        pass
    return urls


async def amain(cfg: Config):
    hub = Hub(cfg)
    app = make_app(hub)

    def _warm():
        try:
            load_whisper(cfg.stt_model, cfg.stt_device)
            print(f"[voicehub] STT 준비 완료 ({cfg.stt_model})")
        except Exception as e:
            print(f"[voicehub] STT 로드 실패: {e}")

    if not cfg.mock:
        threading.Thread(target=_warm, daemon=True, name="voicehub-warm").start()
    ssl_ctx = None
    if cfg.scheme() == "https":
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port, ssl_context=ssl_ctx)
    await site.start()
    print(f"[voicehub] {'HTTPS' if ssl_ctx else 'HTTP'} · 모델 {cfg.model} · STT {cfg.stt_model} · TTS {'mock' if cfg.mock else (cfg.tts_bin or '없음')}")
    for u in local_urls(cfg):
        print(f"[voicehub] 접속: {u}")
    if not ssl_ctx and cfg.host == "0.0.0.0":
        print("[voicehub] 참고: 폰/PC 마이크는 HTTPS 또는 localhost 에서만 열립니다 — 자체서명 인증서는 `autorcode-voice cert` 로 생성")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def main(argv=None):
    p = argparse.ArgumentParser(prog="voicehub", description="음성 대화 허브 (폰·터미널 공용 방)")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--mock", action="store_true", help="STT/LLM/TTS 를 가짜로(테스트용)")
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--model")
    a = p.parse_args(argv)
    cfg = Config(port=a.port, host=a.host, mock=a.mock, tls_cert=a.cert,
                 tls_key=a.key, model=a.model)
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        print("\n[voicehub] 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
