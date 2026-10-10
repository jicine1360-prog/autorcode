"""사진 이해(VLM) — vision.describe 스텁 테스트 + 봇 사진 수신 흐름."""
import json
import os
import shutil
import tempfile
import time
import unittest

from harness import bot as bot_mod
from harness import vision


class _Resp:
    def __init__(self, obj):
        self._obj = obj if isinstance(obj, bytes) else json.dumps(obj).encode()

    def read(self):
        return self._obj

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class VisionTest(unittest.TestCase):
    def setUp(self):
        self._post = vision._post
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "a.png")
        with open(self.img, "wb") as f:
            f.write(b"\x89PNG" + b"0" * 100)

    def tearDown(self):
        vision._post = self._post
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_describe_sends_image_payload(self):
        seen = {}

        def fake_post(url, payload, key, timeout):
            seen["url"] = url
            seen["payload"] = payload
            return {"choices": [{"message": {"content": "붉은 사각형 사진"}}]}

        vision._post = fake_post
        out = vision.describe(self.img, "뭐가 보여?")
        self.assertIn("붉은 사각형", out)
        self.assertTrue(seen["url"].endswith("/chat/completions"))
        content = seen["payload"]["messages"][0]["content"]
        self.assertEqual(content[0]["text"], "뭐가 보여?")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_missing_file_and_bad_ext(self):
        self.assertIn("찾지 못", vision.describe("/nope.png"))
        t = os.path.join(self.tmp, "x.txt")
        open(t, "w").write("x")
        self.assertIn("지원하지 않는", vision.describe(t))

    def test_error_response_is_text(self):
        def fail(url, payload, key, timeout):
            raise RuntimeError("connection refused")
        vision._post = fail
        self.assertIn("[오류]", vision.describe(self.img))

    def test_list_content_parts(self):
        vision._post = lambda u, p, k, t: {"choices": [{"message": {"content": [
            {"type": "text", "text": "파트1"}, {"type": "text", "text": "파트2"}]}}]}
        self.assertEqual(vision.describe(self.img), "파트1 파트2")


class BotPhotoTest(unittest.TestCase):
    def setUp(self):
        self._post = vision._post
        vision._post = lambda u, p, k, t: {"choices": [{"message": {"content": "영수증 사진"}}]}
        self.tmp = tempfile.mkdtemp()
        self.runs = []

        class A:
            def run(self_inner, task):
                self.runs.append(task)
                return "OK"

        def opener(req, timeout=None):
            url = req.full_url
            if "getFile" in url:
                return _Resp({"ok": True, "result": {"file_path": "photos/f.jpg"}})
            if "/file/bot" in url:
                return _Resp(b"IMGBYTES")
            return _Resp({"ok": True, "result": {"message_id": 1}})

        self.bot = bot_mod.Bot("t", [50735853], opener=opener,
                               agent_factory=lambda: A(), workspace=self.tmp)
        self.bot._call = lambda m, p: {"file_path": "photos/f.jpg"} if m == "getFile" else {}

    def tearDown(self):
        vision._post = self._post
        self.runs.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_photo_message_flows_to_agent(self):
        update = {"update_id": 1, "message": {
            "chat": {"id": 50735853},
            "photo": [{"file_id": "f1", "file_size": 10}],
            "caption": "이거 뭐야?"}}
        self.bot._dispatch(update)
        t0 = time.time()
        while not self.runs and time.time() - t0 < 2:
            time.sleep(0.02)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("사진 도착", self.runs[0])
        self.assertIn("이거 뭐야?", self.runs[0])
        files = [f for f in os.listdir(self.tmp) if f.startswith("inbox_")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp, files[0]), "rb") as f:
            self.assertEqual(f.read(), b"IMGBYTES")

    def test_not_allowed_ignored(self):
        self.bot._dispatch({"message": {"chat": {"id": 999},
                                        "photo": [{"file_id": "x", "file_size": 1}]}})
        time.sleep(0.05)
        self.assertEqual(self.runs, [])


if __name__ == "__main__":
    unittest.main()
