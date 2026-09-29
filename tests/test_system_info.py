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

    def test_has_schema_description(self):
        # ALLOW_LIST 에 있어도 SCHEMAS 에 설명이 없으면 /tools/show 목록에서
        # 조용히 사라진다. 실제로 이 형태로 한 번 빠진 적이 있다.
        self.assertIn("system_info", tools.SCHEMAS)
        self.assertIn("section", tools.SCHEMAS["system_info"])

    def test_every_registered_tool_has_a_schema(self):
        # TOOLS 와 SCHEMAS 는 서로 다른 dict 라 서로 조용히 어긋난다.
        # 새로 도구를 붙일 때 설명을 빠뜨리지 못하게 여기서 막는다.
        missing = sorted(set(tools.TOOLS) - set(tools.SCHEMAS))
        self.assertEqual(missing, [], f"구현은 됐지만 설명이 없는 도구: {missing}")

    def test_allow_list_only_references_callable_tools(self):
        # 브리지 허용 목록은 구현이 있는 도구만 가리켜야 한다.
        from harness import bridge, mcp
        available = set(tools.TOOLS) | set(mcp.mcp_tool_fns())
        unknown = sorted(bridge.ALLOW_LIST - available)
        self.assertEqual(unknown, [], f"허용됐지만 구현이 없는 도구: {unknown}")

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
