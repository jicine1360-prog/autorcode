"""MCP 클라이언트 — 가짜 stdio 서버로 프로토콜/노출/실행을 검증(네트워크 불필요)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from harness import mcp, tools

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "echo_mcp.py")


class McpClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "mcp.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"echo": {"command": sys.executable,
                                               "args": [FIXTURE]}}}, f)
        os.environ["AUTORCODE_MCP_CONFIG"] = self.cfg
        os.environ.pop("AUTORCODE_MCP_DISABLE", None)
        mcp.clear_cache()

    def tearDown(self):
        os.environ.pop("AUTORCODE_MCP_CONFIG", None)
        mcp.clear_cache()
        import shutil as _s
        _s.rmtree(self.tmp, ignore_errors=True)

    def test_tool_specs_discovered(self):
        specs = mcp.tool_specs(refresh=True)
        self.assertIn("mcp_echo_echo", specs)
        self.assertEqual(specs["mcp_echo_echo"]["tool"], "echo")
        self.assertEqual(specs["mcp_echo_echo"]["inputSchema"]["type"], "object")

    def test_execute_routes_to_mcp(self):
        out = tools.execute("mcp_echo_echo", {"text": "안녕"}, None, 4000, 10)
        self.assertIn("echo:안녕", out)

    def test_visible_in_text_schema(self):
        mcp.clear_cache()
        self.assertIn("mcp_echo_echo", tools.schema_text())

    def test_native_schema_uses_mcp_inputschema(self):
        names = {t["function"]["name"]: t for t in tools.native_schemas()}
        self.assertIn("mcp_echo_echo", names)
        params = names["mcp_echo_echo"]["function"]["parameters"]
        self.assertIn("text", params.get("properties", {}))

    def test_disable_env_hides_tools(self):
        os.environ["AUTORCODE_MCP_DISABLE"] = "1"
        self.assertNotIn("mcp_echo_echo", tools.schema_text())

    def test_missing_config_is_empty(self):
        os.environ["AUTORCODE_MCP_CONFIG"] = os.path.join(self.tmp, "none.json")
        mcp.clear_cache()
        self.assertEqual(mcp.tool_specs(refresh=True), {})

    def test_server_failure_skipped(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"bad": {"command": "/nonexistent/bin"}}}, f)
        mcp.clear_cache()
        self.assertEqual(mcp.tool_specs(refresh=True), {})


if __name__ == "__main__":
    unittest.main()
