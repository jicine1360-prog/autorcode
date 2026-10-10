"""음성 STT(voice) — transcribe 래퍼 + 봇 음성 메모 흐름 테스트."""
import os
import shutil
import tempfile
import time
import unittest

from harness import bot as bot_mod
from harness import voice


def _fake_runner(text):
    def run(argv, timeout):
        outdir = argv[argv.index("--output_dir") + 1]
        stem = os.path.splitext(os.path.basename(argv[1]))[0]
        with open(os.path.join(outdir, stem + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)
        return 0, ""
    return run


class VoiceTranscribeTest(unittest.TestCase):
    def setUp(self):
        self._real_bin = voice._bin
        voice._bin = lambda: "/usr/bin/fake-whisper"
        self.tmp = tempfile.mkdtemp()
        self.wav = os.path.join(self.tmp, "v.ogg")
        with open(self.wav, "wb") as f:
            f.write(b"OGGS")

    def tearDown(self):
        voice._bin = self._real_bin
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_transcribe_returns_text(self):
        out = voice.transcribe(self.wav, runner=_fake_runner("약속 시간 알려줘"))
        self.assertEqual(out, "약속 시간 알려줘")

    def test_missing_file(self):
        out = voice.transcribe(os.path.join(self.tmp, "nope.ogg"),
                               runner=_fake_runner("x"))
        self.assertIn("찾지 못", out)

    def test_no_whisper_binary(self):
        voice._bin = lambda: None
        self.assertIn("whisper", voice.transcribe(self.wav,
                                                  runner=_fake_runner("x")))

    def test_rc_error(self):
        def run(argv, timeout):
            return 1, "boom"
        self.assertIn("rc=1", voice.transcribe(self.wav, runner=run))

    def test_run_env_adds_user_bins(self):
        env = voice._run_env()
        self.assertIn(os.path.expanduser("~/bin"), env["PATH"].split(os.pathsep))
        self.assertIn(os.path.expanduser("~/.local/bin"), env["PATH"].split(os.pathsep))


class _Resp:
    def __init__(self, obj):
        self._obj = obj if isinstance(obj, bytes) else __import__("json").dumps(obj).encode()

    def read(self):
        return self._obj

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BotVoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.runs = []
        self.sends = []
        self._vt = bot_mod.voice_transcribe
        bot_mod.voice_transcribe = lambda p: "내일 세 시 약속 잡아줘"

        def opener(req, timeout=None):
            url = req.full_url
            if "/file/bot" in url:
                return _Resp(b"OGGDATA")
            return _Resp({"ok": True, "result": {}})

        class A:
            def run(self_inner, task):
                self.runs.append(task)
                return "done"

        self.bot = bot_mod.Bot("t", [50735853], opener=opener,
                               agent_factory=lambda: A(), workspace=self.tmp)
        self.bot._call = lambda m, p: ({"file_path": "voice/f.ogg"}
                                       if m == "getFile" else {})

    def tearDown(self):
        bot_mod.voice_transcribe = self._vt
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_voice_becomes_task(self):
        self.bot._dispatch({"message": {
            "chat": {"id": 50735853},
            "voice": {"file_id": "v1", "file_size": 10}}})
        t0 = time.time()
        while not self.runs and time.time() - t0 < 2:
            time.sleep(0.02)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("내일 세 시 약속 잡아줘", self.runs[0])
        files = [f for f in os.listdir(self.tmp) if f.startswith("voice_")]
        self.assertEqual(len(files), 1)

    def test_empty_speech_not_a_task(self):
        bot_mod.voice_transcribe = lambda p: ""
        self.bot._dispatch({"message": {
            "chat": {"id": 50735853},
            "voice": {"file_id": "v2", "file_size": 1}}})
        time.sleep(0.15)
        self.assertEqual(self.runs, [])

    def test_stt_error_message(self):
        bot_mod.voice_transcribe = lambda p: "[오류] whisper rc=1: boom"
        self.bot._dispatch({"message": {
            "chat": {"id": 50735853},
            "voice": {"file_id": "v3", "file_size": 1}}})
        time.sleep(0.15)
        self.assertEqual(self.runs, [])


if __name__ == "__main__":
    unittest.main()
