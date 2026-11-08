"""todo_write 작업 계획 목록 테스트 (네트워크 불필요)."""
import os
import tempfile
import unittest

from harness import todos, tools


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        self._old_mcp = os.environ.get("AUTORCODE_MCP_DISABLE")
        os.environ["AUTORCODE_MCP_DISABLE"] = "1"  # 네이티브 스키마 수집 시 MCP 기동 금지

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        if self._old_mcp is None:
            os.environ.pop("AUTORCODE_MCP_DISABLE", None)
        else:
            os.environ["AUTORCODE_MCP_DISABLE"] = self._old_mcp

    def test_write_render_counts(self):
        out = todos.write(self.ws, [
            {"content": "가", "status": "done"},
            {"content": "나", "status": "doing"},
            {"content": "다"},
        ])
        self.assertIn("[1/3 완료]", out)
        self.assertIn("☑ 1. 가", out)
        self.assertIn("▶ 2. 나", out)
        items = todos.load(self.ws)
        self.assertEqual([i["status"] for i in items], ["done", "doing", "pending"])

    def test_status_aliases_and_multi_doing_demoted(self):
        out = todos.write(self.ws, [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "completed"},
        ])
        items = todos.load(self.ws)
        self.assertEqual([i["status"] for i in items], ["doing", "pending", "done"])
        self.assertIn("doing 단계가 2개 이상", out)

    def test_replace_all_semantics_and_clear(self):
        todos.write(self.ws, [{"content": "x"}])
        todos.write(self.ws, [{"content": "y"}, {"content": "z"}])
        self.assertEqual([i["content"] for i in todos.load(self.ws)], ["y", "z"])
        todos.write(self.ws, [])
        self.assertEqual(todos.load(self.ws), [])

    def test_per_workspace_isolation(self):
        ws2 = os.path.join(self.tmp, "ws2")
        os.makedirs(ws2)
        todos.write(self.ws, [{"content": "여기 것"}])
        self.assertEqual(todos.load(ws2), [])
        self.assertEqual(len(todos.load(self.ws)), 1)

    def test_has_open(self):
        todos.write(self.ws, [{"content": "a", "status": "done"}])
        self.assertFalse(todos.has_open(self.ws))
        todos.write(self.ws, [{"content": "a", "status": "done"}, {"content": "b"}])
        self.assertTrue(todos.has_open(self.ws))

    def test_note_roundtrip_and_plain_strings_accepted(self):
        out = todos.write(self.ws, ["단계 하나", {"content": "둘", "status": "done"}], note="마감 금요일")
        self.assertIn("메모: 마감 금요일", out)
        self.assertEqual(todos.load_note(self.ws), "마감 금요일")
        self.assertEqual([i["content"] for i in todos.load(self.ws)], ["단계 하나", "둘"])

    def test_execute_via_tools_registry(self):
        out = tools.execute("todo_write",
                            {"todos": [{"content": "t", "status": "done"}]},
                            self.ws, 8000, 5)
        self.assertIn("[1/1 완료]", out)
        missing = tools.execute("todo_write", {}, self.ws, 8000, 5)
        self.assertIn("[오류]", missing)

    def test_native_schema_exposes_todo_write(self):
        spec = {s["function"]["name"]: s for s in tools.native_schemas()}
        self.assertIn("todo_write", spec)
        params = spec["todo_write"]["function"]["parameters"]
        self.assertEqual(params["properties"]["todos"]["type"], "array")
        self.assertIn("todos", params["required"])


if __name__ == "__main__":
    unittest.main()
