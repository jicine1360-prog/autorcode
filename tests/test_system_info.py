"""system_info 도구 테스트 — 라이브 상태 조회가Sections 분리/방어적으로 동작하는지."""
import unittest

from harness import tools


class SystemInfoTest(unittest.TestCase):
    def _info(self, **args):
        return tools.execute("system_info", args, "/home/hoony", 8000, 20)

    def test_registered_in_tools(self):
        self.assertIn("system_info", tools.TOOLS)

    def test_registered_in_bridge_allow_list(self):
        from harness import bridge
        self.assertIn("system_info", bridge.ALLOW_LIST)

    def test_default_reports_live_sections(self):
        out = self._info()
        for tag in ("[호스트]", "[CPU]", "[메모리]", "[GPU]", "[모델]", "[디스크]"):
            self.assertIn(tag, out)

    def test_single_section_only(self):
        out = self._info(section="cpu")
        self.assertIn("[CPU]", out)
        self.assertNotIn("[디스크]", out)

    def test_unknown_section_falls_back_to_all(self):
        # 존재하지 않는 섹션이 조용히 빈 문자열을 돌려주면 모델이 "값이 없다"고
        # 오해한다. 전량으로 내려가는 편이 정직하다.
        out = self._info(section="존재하지않음")
        self.assertIn("[호스트]", out)
        self.assertIn("[디스크]", out)

    def test_every_section_succeeds_individually(self):
        for sec in tools._SYS_SECTIONS:
            with self.subTest(section=sec):
                out = self._info(section=sec)
                self.assertTrue(out.strip())
                self.assertNotIn("Traceback", out)

    def test_reports_running_model_not_installed_list(self):
        # 도구가 답해야 하는 건 '무엇이 설치돼 있나'가 아니라 '무엇이 돌고 있나'.
        out = self._info(section="models")
        self.assertIn("[모델]", out)
        self.assertNotIn("[오류]", out)

    def test_respects_output_cap(self):
        capped = tools.execute("system_info", {}, "/home/hoony", 60, 20)
        self.assertLessEqual(len(capped), 200)

    def test_reports_hostname(self):
        out = self._info(section="host")
        self.assertIn("[호스트]", out)


if __name__ == "__main__":
    unittest.main()
