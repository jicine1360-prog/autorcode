"""PII 마스킹 — 패턴/복원/적용 판정 테스트."""
import unittest

from harness import privacy


class PrivacyTest(unittest.TestCase):
    def test_masks_known_patterns(self):
        text = "연락처 010-1234-5678, 메일 a@b.co.kr, 번호 811010-1234567, 카드 1234-5678-9012-3456"
        masked, mapping = privacy.mask(text)
        self.assertNotIn("010-1234-5678", masked)
        self.assertNotIn("a@b.co.kr", masked)
        self.assertNotIn("811010-1234567", masked)
        self.assertNotIn("1234-5678-9012-3456", masked)
        self.assertEqual(len(mapping), 4)

    def test_restore_roundtrip(self):
        masked, mapping = privacy.mask("이메일 kx@zz.kr 입니다")
        self.assertIn("<PII:email-1>", masked)
        self.assertEqual(privacy.restore(masked, mapping), "이메일 kx@zz.kr 입니다")

    def test_mask_messages(self):
        tool_text = "전화 010-1111-2222 기록"
        msgs = [{"role": "user", "content": "010-9999-9999"},
                {"role": "tool", "content": tool_text}]
        out, mapping = privacy.mask_messages(msgs)
        self.assertNotIn("010-9999-9999", out[0]["content"])
        self.assertNotIn("010-1111-2222", out[1]["content"])
        self.assertEqual(privacy.restore(out[1]["content"], mapping), tool_text)
        self.assertEqual(len(mapping), 2)
        self.assertEqual(msgs[1]["content"], tool_text)  # 원본 리스트는 안 변함

    def test_same_value_reuses_placeholder(self):
        m1 = {"role": "user", "content": "010-1111-2222"}
        m2 = {"role": "tool", "content": "010-1111-2222"}
        out, mapping = privacy.mask_messages([m1, m2])
        self.assertEqual(out[0]["content"], out[1]["content"])
        self.assertEqual(len(mapping), 1)

    def test_should_mask(self):
        self.assertFalse(privacy.should_mask("http://127.0.0.1:11434/v1"))
        self.assertFalse(privacy.should_mask("http://localhost:11434/v1"))
        self.assertTrue(privacy.should_mask("https://openrouter.ai/api/v1"))
        self.assertTrue(privacy.should_mask("http://127.0.0.1:11434/v1", "on"))
        self.assertFalse(privacy.should_mask("https://cloud.example", "off"))


if __name__ == "__main__":
    unittest.main()