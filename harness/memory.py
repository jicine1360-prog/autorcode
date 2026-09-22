"""컨텍스트 관리 — 토큰 추정 예산 기반 트리밍 + 사용량 집계."""
import logging
from typing import Dict, List, Optional

from . import tokens

log = logging.getLogger("agent.memory")


def estimate(text: str) -> int:
    return tokens.count(text)


class Memory:
    def __init__(self, system: str, budget_tokens: int):
        self.system = system
        self.budget = budget_tokens
        self.turns: List[Dict[str, str]] = []
        self._dropped = 0
        self.on_add = None  # callable(entry) — 세션 영속화 훅

    def add(self, role: str, content: str, stats: Optional[dict] = None) -> None:
        entry = {"role": role, "content": content}
        self.turns.append(entry)
        if self.on_add:
            self.on_add(entry)
        self.trim()

    def messages(self) -> List[Dict[str, str]]:
        note = ""
        if self._dropped:
            note = f"\n(컨텍스트 제한으로 이전 턴 {self._dropped}개 생략됨)"
        return [{"role": "system", "content": self.system + note}] + list(self.turns)

    def size(self) -> int:
        return sum(estimate(m["content"]) for m in self.turns) + estimate(self.system)

    def trim(self) -> None:
        while self.size() > self.budget and len(self.turns) > 4:
            start = None
            for i in range(len(self.turns) - 4):
                m = self.turns[i]
                if m["role"] == "user" and not m["content"].startswith("[도구 결과]"):
                    start = i
                    break
            if start is None:
                self.turns.pop(0)
                self._dropped += 1
                continue
            j = start + 1
            while j < len(self.turns) - 4:
                nxt = self.turns[j]
                if nxt["role"] == "user" and not nxt["content"].startswith("[도구 결과]"):
                    break
                j += 1
            del self.turns[start:j]
            self._dropped += (j - start)
            log.debug("트리밍: %d 메시지 드롭 (현재 %dtok)", j - start, self.size())
