#!/usr/bin/env python3
"""테스트용 최소 MCP 서버 (stdio) — 표준 라이브러리만 사용."""
import json
import sys

TOOLS = [
    {"name": "add", "description": "두 수를 더한다",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "echo", "description": "문자열을 그대로 돌려준다",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "0.1"}}}
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            p = msg.get("params", {})
            name, args = p.get("name"), p.get("arguments", {})
            if name == "add":
                text = str(args.get("a", 0) + args.get("b", 0))
            elif name == "echo":
                text = args.get("text", "")
            else:
                resp = {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": f"unknown {name}"}]}}
                print(json.dumps(resp, ensure_ascii=False), flush=True)
                continue
            resp = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
        else:
            if mid is None:
                continue
            resp = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}}
        print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
