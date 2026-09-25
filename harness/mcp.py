"""MCP (Model Context Protocol) 클라이언트 — 남들 만든 도구 생태계를 autorcode에 연결한다.

설정: ~/.autorcode/mcp.json
{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/hoony"]}
  }
}

동작 방식
- stdio 트랜스포트: 서버 프로세스를 띄우고 stdin/stdout으로 newline-delimited JSON-RPC 교환
- initialize → notifications/initialized → tools/list → tools/call
- 도구 이름은 "mcp_<서버명>_<도구명>"으로 충돌 없이 등록
- 보안: MCP 도구는 autorcode의 화이트리스트/샌드박스를 우회한다(서버가 직접 실행).
  신뢰하는 서버만 등록할 것.
"""
import json
import logging
import os
import subprocess
import threading
import time
from typing import Dict

log = logging.getLogger("agent.mcp")

PROTOCOL_VERSION = "2024-11-05"
CONFIG_PATH = os.path.expanduser("~/.autorcode/mcp.json")
_TIMEOUT = float(os.getenv("AUTORCODE_MCP_TIMEOUT", "60"))


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcpServers")
        return servers if isinstance(servers, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("mcp.json 파싱 실패: %s", e)
        return {}


class _RpcError(RuntimeError):
    pass


class StdioClient:
    """서버 프로세스당 한 번의 연산(initialize→call)을 수행하고 닫는다."""

    def __init__(self, command, args, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        self._proc = subprocess.Popen(
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=full_env,
        )
        self._lock = threading.Lock()
        self._next_id = 0

    def _send(self, payload: dict) -> None:
        assert self._proc.stdin
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict:
        assert self._proc.stdout
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise _RpcError("서버가 응답 없이 종료됨 (command/args 확인)")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "id" in msg:
                return msg

    def request(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            self._send(req)
            deadline = time.time() + _TIMEOUT
            while True:
                if time.time() > deadline:
                    raise _RpcError(f"{method} 타임아웃 ({_TIMEOUT:.0f}초)")
                resp = self._read()
                if resp.get("id") != rid:
                    continue
                if "error" in resp:
                    raise _RpcError(str(resp["error"]))
                return resp.get("result") or {}

    def notify(self, method: str) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method})

    def initialize(self) -> None:
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "autorcode", "version": "2.1.0"},
        })
        self.notify("notifications/initialized")

    def list_tools(self) -> list:
        return self.request("tools/list").get("tools") or []

    def call(self, tool: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            texts = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
            return f"[오류] {tool}: {texts[:400]}"
        parts = []
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append(json.dumps(c, ensure_ascii=False))
        return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()
        finally:
            self._proc.stdout and self._proc.stdout.close()


def _server_spec(server_name: str) -> dict:
    return _load_config().get(server_name) or {}


def _client(server_name: str) -> StdioClient:
    spec = _server_spec(server_name)
    if not spec.get("command"):
        raise _RpcError(f"mcp.json에 서버 없음: {server_name!r} ({CONFIG_PATH})")
    c = StdioClient(spec["command"], spec.get("args") or [], spec.get("env"))
    c.initialize()
    return c


def mcp_servers() -> dict:
    """{서버명: [도구 설명 요약]} — CLI/진단용."""
    out: Dict[str, list] = {}
    for name in _load_config():
        try:
            c = _client(name)
            out[name] = [t.get("name", "?") for t in c.list_tools()]
            c.close()
        except Exception as e:
            out[name] = [f"[연결 실패: {e}]"]
    return out


def _tool_fn(server_name: str, tool_name: str, description: str):
    def fn(args: dict, root: str, max_output: int, timeout: int) -> str:
        c = _client(server_name)
        try:
            out = c.call(tool_name, args if isinstance(args, dict) else {})
            return out[:max_output] if max_output else out
        finally:
            c.close()
    fn.__name__ = f"mcp_{server_name}_{tool_name}"
    fn.__doc__ = description or f"MCP {server_name}/{tool_name}"
    return fn


def mcp_tool_fns() -> Dict[str, object]:
    """전 서버의 도구를 "mcp_<서버>_<도구>" 이름으로 수집. 연결 실패 서버는 건너뛴다."""
    fns: Dict[str, object] = {}
    for name in _load_config():
        try:
            c = _client(name)
            for t in c.list_tools():
                tname = t.get("name")
                if tname:
                    fns[f"mcp_{name}_{tname}"] = _tool_fn(name, tname, t.get("description") or "")
            c.close()
        except Exception as e:
            log.warning("MCP 서버 %s 연결 실패: %s", name, e)
    return fns
