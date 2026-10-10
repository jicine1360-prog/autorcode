#!/usr/bin/env python3
"""테스트용 최소 MCP 서버(stdio, newline-delimited JSON-RPC).

도구 1개(echo)를 제공한다. initialize / tools/list / tools/call 만 처리.
표준 라이브러리만 사용한다.
"""
import json
import sys

PROTOCOL = "2024-11-05"
TOOLS = [{
    "name": "echo",
    "description": "받은 text 를 그대로 메아리로 돌려준다",
    "inputSchema": {"type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
}]


def _reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            _reply(rid, {"protocolVersion": PROTOCOL,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "echo", "version": "0.1"}})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue  # 알림 — 응답 없음
        elif method == "tools/list":
            _reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            _reply(rid, {"content": [{"type": "text",
                                      "text": "echo:" + str(args.get("text", ""))}],
                         "isError": False})
        elif rid is not None:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601, "message": f"unknown {method}"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
