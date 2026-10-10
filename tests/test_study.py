"""미리 학습(study) — 저장/조회/힌트 + 도구 래퍼 테스트 (네트워크 불필요)."""
import os
import shutil
import tempfile
import unittest

from harness import notes, study, tools


class StudyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AUTORCODE_STUDY_DIR"] = self.tmp
        self.chat = 50735853

    def tearDown(self):
        os.environ.pop("AUTORCODE_STUDY_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_recall_roundtrip(self):
        study.save(self.chat, "전기차 배터리", "LFP vs NCM: LFP는 저렴·안전, NCM은 고에너지밀도.")
        out = study.recall(self.chat)
        self.assertIn("전기차 배터리", out)
        self.assertIn("LFP", out)

    def test_recall_single_topic(self):
        study.save(self.chat, "A", "가")
        study.save(self.chat, "B", "나")
        self.assertIn("나", study.recall(self.chat, "B"))
        self.assertNotIn("가", study.recall(self.chat, "B"))
        self.assertIn("준비된 주제", study.recall(self.chat, "C"))

    def test_same_topic_replaced(self):
        study.save(self.chat, "A", "옛내용")
        study.save(self.chat, "A", "새내용")
        out = study.recall(self.chat, "A")
        self.assertIn("새내용", out)
        self.assertNotIn("옛내용", out)

    def test_per_chat_separated(self):
        study.save(self.chat, "A", "내 것")
        self.assertIn("학습 자료 없음", study.recall(self.chat + 1))

    def test_hint_none_when_empty(self):
        self.assertIsNone(study.hint(self.chat))
        study.save(self.chat, "파이썬", "데코레이터...")
        self.assertIn("study_recall", study.hint(self.chat))

    def test_empty_inputs_rejected(self):
        self.assertIn("오류", study.save(self.chat, "", "x"))
        self.assertIn("오류", study.save(self.chat, "t", "  "))


class StudyToolTest(StudyStoreTest):
    def test_tools_scope_to_current_chat(self):
        with notes.scope(str(self.chat)):
            tools.execute("study_save", {"topic": "제주 여행", "content": "성산일출봉 추천"}, None, 4000, 10)
            out = tools.execute("study_recall", {}, None, 4000, 10)
        self.assertIn("성산일출봉", out)


if __name__ == "__main__":
    unittest.main()