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


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, timeout: int,
                 retries: int, temperature: float, max_tokens: int = 800):
        if not base_url.startswith(("http://", "https://")):
            raise LLMError(f"잘못된 base_url: {base_url!r}")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(self, messages: List[ChatMessage], model: str) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
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
                    body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    detail = ""
                last_err = f"HTTP {e.code}: {detail}"
                log.warning("LLM %s 오류 (시도 %d/%d) %s", e.code, attempt, self.retries, detail)
                if e.code in (400, 401, 403, 404):  # 재시도 무의미
                    raise LLMError(last_err)
            except Exception as e:  # 네트워크/타임아웃/JSON
                last_err = str(e)
                log.warning("LLM 호출 실패 (시도 %d/%d): %s", attempt, self.retries, e)
            if attempt < self.retries:
                time.sleep(backoff)
                backoff *= 2
        raise LLMError(f"LLM 호출 {self.retries}회 실패: {last_err}")


class MockModel:
    """규칙 기반 더미 플래너. 도구 호출 JSON 규식을 충실히 따르며,
    [도구 결과] 관측을 하나 받으면 종료한다. 루프 자체는 실제와 동일하게 돈다."""

    def chat(self, messages: List[ChatMessage], model: str) -> str:
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
