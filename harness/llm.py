"""LLM 클라이언트.

- OpenAICompatibleLLM: stdlib urllib만으로 호출, 429/5xx 지수백오프 재시도, 4xx 즉시 실패.
- MockModel: API 키 없이 루프/도구/안전 계층을 검증하는 오프라인 더미 플래너.

실전에서는 MockModel 클래스를 지우면 되고, 프로바이더는
AGENT_BASE_URL/AGENT_API_KEY 환경변수로 교체한다 (OpenAI, OpenRouter, vLLM, ollama 등).
"""
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List

log = logging.getLogger("agent.llm")

ChatMessage = Dict[str, str]


class LLMError(RuntimeError):
    pass


class LengthError(LLMError):
    """출력 토큰 상한에 걸렸지만 회복 가능하다 — 더 짧게 재요청한다."""


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, timeout: int,
                 retries: int, temperature: float, max_tokens: int = 2048):
        if not base_url.startswith(("http://", "https://")):
            raise LLMError(f"잘못된 base_url: {base_url!r}")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(self, messages: List[ChatMessage], model: str, *, stream=False,
             on_event=None) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        backoff = 2.0
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(self.endpoint, data=data,
                                             headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if "text/event-stream" in resp.headers.get("Content-Type", ""):
                        return self._read_stream(resp, on_event)
                    # stream 옵션을 무시하고 JSON을 반환하는 서버도 지원한다.
                    body = json.loads(resp.read(4_000_001).decode("utf-8"))
                choice = body["choices"][0]
                content = choice["message"].get("content")
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("모델이 빈 응답을 반환했습니다")
                if choice.get("finish_reason") == "length":
                    raise LengthError("응답 길이 제한 도달")
                if on_event:
                    on_event("received", len(content))
                return content
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    detail = ""
                last_err = f"HTTP {e.code}: {detail}"
                log.warning("LLM %s 오류 (시도 %d/%d) %s", e.code, attempt, self.retries, detail)
                if e.code in (400, 401, 403, 404):  # 재시도 무의미
                    raise LLMError(last_err)
            except LLMError:
                raise
            except Exception as e:  # 네트워크/타임아웃/JSON
                last_err = str(e)
                log.warning("LLM 호출 실패 (시도 %d/%d): %s", attempt, self.retries, e)
            if attempt < self.retries:
                if on_event:
                    on_event("retry", f"API 재시도 {attempt + 1}/{self.retries} · {backoff:.0f}s 후")
                time.sleep(backoff)
                backoff *= 2
        raise LLMError(f"LLM 호출 {self.retries}회 실패: {last_err}")

    @staticmethod
    def _read_stream(resp, on_event):
        """SSE를 조립하되 원시 추론 필드는 표시/전달하지 않는다."""
        parts, event_lines = [], []
        received = 0
        total_bytes = 0
        finished = False
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
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str) and content:
                parts.append(content)
                received += len(content)
                if on_event:
                    on_event("received", received)
            reason = choice.get("finish_reason")
            if reason == "length":
                raise LengthError("응답 길이 제한 도달")
            if reason is not None:
                finished = True
        text = "".join(parts)
        if not finished:
            raise LLMError("응답 스트림이 완료 신호 없이 끊겼습니다")
        if not text.strip():
            raise LLMError("모델이 빈 응답을 반환했습니다")
        return text


class MockModel:
    """규칙 기반 더미 플래너. 도구 호출 JSON 규식을 충실히 따르며,
    [도구 결과] 관측을 하나 받으면 종료한다. 루프 자체는 실제와 동일하게 돈다."""

    def chat(self, messages: List[ChatMessage], model: str, *, stream=False,
             on_event=None) -> str:
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
            return json.dumps(self._plan(task), ensure_ascii=False)
        if task:
            return json.dumps({
                "thought": "관측 확인 → 종료",
                "done": True,
                "answer": f"[mock:{model}] 작업 완료. 최종 관측: {last_obs[:160]}",
            }, ensure_ascii=False)
        return json.dumps({
            "done": True, "answer": "[mock] 할 일을 입력하세요.",
        }, ensure_ascii=False)

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
