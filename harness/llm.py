"""LLM 클라이언트.

- OpenAICompatibleLLM: stdlib urllib만으로 호출, 429/5xx 지수백오프 재시도, 4xx 즉시 실패.
- MockModel: API 키 없이 루프/도구/안전 계층을 검증하는 오프라인 더미 플래너.
- Reply: chat() 반환. content(텍스트) + tool_calls(네이티브 function calling).

네이티브 도구: tools= 로 스키마를 넘기면 스트리밍/비스트리밍 모두 delta/message 의
tool_calls 를 조립해 Reply.tool_calls 로 돌려준다. 서버가 tools 파라미터를 거부하면
한 번 tools 없이 재시도한다 (JSON 규식 폴백).

실전에서는 MockModel 클래스를 지우면 되고, 프로바이더는
AGENT_BASE_URL/AGENT_API_KEY 환경변수로 교체한다 (OpenAI, OpenRouter, vLLM, ollama 등).
"""
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("agent.llm")

ChatMessage = Dict[str, Any]


class LLMError(RuntimeError):
    pass


class LengthError(LLMError):
    """출력 토큰 상한에 걸렸지만 회복 가능하다 — 더 짧게 재요청한다."""


@dataclass
class Reply:
    content: str
    tool_calls: Optional[List[dict]] = None
    finish_reason: Optional[str] = None


def _tool_calls_from_message(message: dict) -> Optional[List[dict]]:
    """message 딕셔너리의 tool_calls 를 정규화한다 (arguments 는 str 보장)."""
    tcs = message.get("tool_calls")
    if not isinstance(tcs, list) or not tcs:
        return None
    out = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        fn = dict(fn)
        arguments = fn.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        fn["arguments"] = arguments if isinstance(arguments, str) else ""
        out.append({"id": tc.get("id") or "",
                    "type": tc.get("type") or "function",
                    "function": fn})
    return out or None


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, timeout: int,
                 retries: int, temperature: float, max_tokens: int = 8192,
                 reasoning_effort: str = ""):
        if not base_url.startswith(("http://", "https://")):
            raise LLMError(f"잘못된 base_url: {base_url!r}")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort

    def chat(self, messages: List[ChatMessage], model: str, *, stream=False,
             on_event=None, tools=None) -> Reply:
        """tools: OpenAI function 스키마 목록. 서버가 거부하면 tools 없이 재시도."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        backoff = 2.0
        last_err = None
        dropped_tools = False
        for attempt in range(1, self.retries + 2):  # +1: tools 없이 재시도 여유
            try:
                req = urllib.request.Request(self.endpoint, data=data,
                                             headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if "text/event-stream" in resp.headers.get("Content-Type", ""):
                        return self._read_stream(resp, on_event)
                    # stream 옵션을 무시하고 JSON을 반환하는 서버도 지원한다.
                    body = json.loads(resp.read(4_000_001).decode("utf-8"))
                choice = body["choices"][0]
                return self._parse_json_choice(choice, on_event)
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    detail = ""
                last_err = f"HTTP {e.code}: {detail}"
                log.warning("LLM %s 오류 (시도 %d) %s", e.code, attempt, detail)
                if tools and e.code in (400, 404) and not dropped_tools:
                    # 서버가 이 모델에 tools 를 지원하지 않음 → tools 없이 폴백
                    log.warning("서버가 tools 파라미터를 거부 — tools 없이 재시도")
                    dropped_tools = True
                    tools = None
                    payload.pop("tools", None)
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    continue
                if e.code in (400, 401, 403, 404):  # 재시도 무의미
                    raise LLMError(last_err)
            except LLMError:
                raise
            except Exception as e:  # 네트워크/타임아웃/JSON
                last_err = str(e)
                log.warning("LLM 호출 실패 (시도 %d): %s", attempt, e)
            if attempt < self.retries:
                if on_event:
                    on_event("retry", f"API 재시도 {attempt + 1}/{self.retries} · {backoff:.0f}s 후")
                time.sleep(backoff)
                backoff *= 2
        raise LLMError(f"LLM 호출 {self.retries}회 실패: {last_err}")

    @staticmethod
    def _parse_json_choice(choice: dict, on_event) -> Reply:
        message = choice.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        tool_calls = _tool_calls_from_message(message)
        if not content.strip() and not tool_calls:
            raise LLMError("모델이 빈 응답을 반환했습니다")
        if choice.get("finish_reason") == "length":
            raise LengthError("응답 길이 제한 도달")
        if on_event and content:
            on_event("received", len(content))
        return Reply(content, tool_calls=tool_calls,
                     finish_reason=choice.get("finish_reason"))

    @staticmethod
    def _read_stream(resp, on_event):
        """SSE를 조립하되 원시 추론 필드는 표시/전달하지 않는다. tool_calls 도 조립한다."""
        parts, event_lines = [], []
        received = 0
        total_bytes = 0
        finished = False
        last_reason = None
        tool_acc: Dict[int, dict] = {}
        while True:
            raw = resp.readline(1_000_001)
            total_bytes += len(raw)
            if len(raw) > 1_000_000 or total_bytes > 4_000_000:
                raise LLMError("모델 응답이 수신 상한을 초과했습니다")
            if not raw:
                if event_lines:
                    raise LLMError("스트림이 이벤트 중간에 끊겼습니다")
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                event_lines.append(line[5:].lstrip(" "))
                continue
            if line:
                continue
            if not event_lines:
                continue
            data = "\n".join(event_lines)
            event_lines.clear()
            if data == "[DONE]":
                finished = True
                break
            event = json.loads(data)
            if event.get("error"):
                raise LLMError("모델 서버가 스트림 오류를 반환했습니다")
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
                received += len(content)
                if on_event:
                    on_event("received", received)
            tcs = delta.get("tool_calls")
            if isinstance(tcs, list):
                for item in tcs:
                    if not isinstance(item, dict):
                        continue
                    idx = item.get("index", 0)
                    acc = tool_acc.setdefault(
                        int(idx), {"id": "", "type": "function", "name": "", "arguments": ""})
                    if item.get("id"):
                        acc["id"] = item["id"]
                    if item.get("type"):
                        acc["type"] = item["type"]
                    fn = item.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        frag = fn.get("arguments")
                        if isinstance(frag, str) and frag:
                            acc["arguments"] += frag
                            received += len(frag)
                            if on_event:
                                on_event("received", received)
            reason = choice.get("finish_reason")
            if reason:
                last_reason = reason
                if reason == "length":
                    raise LengthError("응답 길이 제한 도달")
                finished = True
                break
        text = "".join(parts)
        if not finished:
            raise LLMError("응답 스트림이 완료 신호 없이 끊겼습니다")
        tool_calls = None
        if tool_acc:
            tool_calls = []
            for idx in sorted(tool_acc):
                acc = tool_acc[idx]
                tool_calls.append({
                    "id": acc["id"],
                    "type": acc["type"],
                    "function": {"name": acc["name"], "arguments": acc["arguments"]},
                })
        if not text.strip() and not tool_calls:
            raise LLMError("모델이 빈 응답을 반환했습니다")
        return Reply(text, tool_calls=tool_calls, finish_reason=last_reason)


class MockModel:
    """규칙 기반 더미 플래너. 도구 호출 JSON 규식을 충실히 따르며,
    [도구 결과] 관측을 하나 받으면 종료한다. 루프 자체는 실제와 동일하게 돈다."""

    def chat(self, messages: List[ChatMessage], model: str, *, stream=False,
             on_event=None, tools=None) -> Reply:
        task = None
        observations = 0
        last_obs = ""
        for m in messages:
            if m["role"] != "user":
                continue
            if m["content"].startswith("[도구 결과]"):
                observations += 1
                last_obs = m["content"]
            else:
                task = m["content"]
                observations = 0

        if task and observations == 0:
            return Reply(json.dumps(self._plan(task), ensure_ascii=False),
                         finish_reason="stop")
        if task:
            return Reply(json.dumps({
                "thought": "관측 확인 → 종료",
                "done": True,
                "answer": f"[mock:{model}] 작업 완료. 최종 관측: {last_obs[:160]}",
            }, ensure_ascii=False), finish_reason="stop")
        return Reply(json.dumps({
            "done": True, "answer": "[mock] 할 일을 입력하세요.",
        }, ensure_ascii=False), finish_reason="stop")

    def _plan(self, task: str) -> dict:
        t = task.lower()
        m = re.search(r"([\w./-]+\.\w+)", task)
        fname = m.group(1) if m else None

        if any(k in t for k in ("목록", "리스트", "ls", "파일 보여", "show files")):
            return {"thought": "디렉터리 조회", "tool": "bash", "args": {"command": "ls -la"}}
        if any(k in t for k in ("시간", "date")):
            return {"thought": "현재 시각", "tool": "bash", "args": {"command": "date"}}
        if fname and any(k in t for k in ("읽", "cat", "보여")) and "쓰" not in t:
            return {"thought": f"{fname} 읽기", "tool": "read_file", "args": {"path": fname}}
        if fname and any(k in t for k in ("쓰", "생성", "만들", "작성", "save")):
            return {"thought": f"{fname} 작성", "tool": "write_file",
                    "args": {"path": fname, "content": f"{task}\n(mock 생성)\n"}}
        if any(k in t for k in ("쓰", "생성", "만들", "작성", "hello")):
            return {"thought": "hello.txt 생성", "tool": "write_file",
                    "args": {"path": "hello.txt", "content": "hello\n"}}
        return {"thought": "작업 없음", "done": True, "answer": f"[mock:{t and 'fast'}] 응답: {task}"}