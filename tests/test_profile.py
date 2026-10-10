"""폰별 사용자 언어(profile) + 사진 번역 기본값 테스트."""
import json
import os
import shutil
import tempfile
import unittest

from harness import bot as bot_mod
from harness import notes, profile, tools, vision


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AUTORCODE_PROFILE_DIR"] = self.tmp
        self.chat = 35121483

    def tearDown(self):
        os.environ.pop("AUTORCODE_PROFILE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_get_roundtrip(self):
        self.assertIn("영어", profile.set_language(self.chat, "english"))
        self.assertEqual(profile.language(self.chat), "영어")

    def test_aliases(self):
        for alias in ("en", "English", "영어"):
            profile.set_language(self.chat, alias)
            self.assertEqual(profile.language(self.chat), "영어")

    def test_unlisted_language_still_accepted(self):
        self.assertIn("포르투갈어", profile.set_language(self.chat, "포르투갈어"))
        self.assertEqual(profile.language(self.chat), "포르투갈어")

    def test_empty_language_rejected(self):
        self.assertIn("오류", profile.set_language(self.chat, "  "))

    def test_per_chat_separated(self):
        profile.set_language(self.chat, "일본어")
        self.assertEqual(profile.language(self.chat), "일본어")
        self.assertEqual(profile.language(self.chat + 1), "한국어")  # 기본값

    def test_tool_wrapper_scoped(self):
        with notes.scope(str(self.chat)):
            out = tools.execute("set_language", {"lang": "스페인어"}, None, 4000, 10)
        self.assertIn("스페인어", out)
        self.assertEqual(profile.language(self.chat), "스페인어")


class PhotoLangFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AUTORCODE_PROFILE_DIR"] = self.tmp
        os.environ["AUTORCODE_LOCATION_DIR"] = self.tmp
        os.environ["AUTORCODE_GOALS_DIR"] = self.tmp
        os.environ["AUTORCODE_STUDY_DIR"] = self.tmp
        profile.set_language(35121483, "영어")
        self._post = vision._post
        self._real_get = vision._get if hasattr(vision, "_get") else None

    def tearDown(self):
        vision._post = self._post
        for k in ("AUTORCODE_PROFILE_DIR", "AUTORCODE_LOCATION_DIR",
                  "AUTORCODE_GOALS_DIR", "AUTORCODE_STUDY_DIR"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_photo_default_request_uses_phone_language(self):
        captured = []

        def opener(req, timeout=None):
            class R:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
                def read(self_inner): return b"BYTES"
            return R()

        b = bot_mod.Bot("t", [35121483], opener=opener, workspace=self.tmp)
        b._call = lambda m, p: {"file_path": "photos/f.jpg"} if m == "getFile" else {}
        b._handle = lambda chat, text: captured.append(text)
        b._on_photo(35121483, {"photo": [{"file_id": "f", "file_size": 9}]}, "")
        self.assertIn("영어", captured[0])

    def test_photo_doc_defaults_to_profile_lang(self):
        seen = {}

        def fake_post(url, payload, key, timeout):
            seen["q"] = payload["messages"][0]["content"][0]["text"]
            return {"choices": [{"message": {"content": "Hello world"}}]}

        vision._post = fake_post
        img = os.path.join(self.tmp, "a.jpg")
        open(img, "wb").write(b"\xff\xd8" + b"0" * 50)
        with notes.scope("35121483"):
            out = tools.execute("photo_doc", {"path": "a.jpg", "task": "translate"},
                                self.tmp, 4000, 10)
        self.assertIn("Hello world", out)
        self.assertIn("영어", seen["q"])


if __name__ == "__main__":
    unittest.main()