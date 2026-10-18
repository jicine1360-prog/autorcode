"""컨텍스트 관리 — 토큰 추정 예산 기반 트리밍 + 사용량 집계."""
import logging
from typing import Dict, List, Optional

from . import tokens

log = logging.getLogger("agent.memory")


def estimate(text: str) -> int:
    return tokens.count(str(text or ""))


def _short(text: str, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class Memory:
    def __init__(self, system: str, budget_tokens: int, summary_max_chars: int = 4000):
        self.system = system
        self.budget = budget_tokens
        self.summary_max_chars = summary_max_chars
        self.summary = ""
        self.turns: List[Dict] = []
        self._dropped = 0
        self.on_add = None  # callable(entry) — 세션 영속화 훅

    def add(self, role: str, content: str, stats: Optional[dict] = None) -> None:
        entry = {"role": role, "content": content}
        self.turns.append(entry)
        if self.on_add:
            self.on_add(entry)
        self.trim()

    def add_assistant_toolcalls(self, tool_calls: list, content: str = "") -> None:
        """네이티브 function calling 라운드: assistant(tool_calls) 메시지를 추가한다.

        결과(role:tool)가 뒤따르므로 여기서 trim/repair 하지 않는다. 반드시
        add_tool_results() 로 페어링을 완성한 뒤 트리밍한다.
        """
        entry = {"role": "assistant", "content": content,
                 "tool_calls": list(tool_calls)}
        self.turns.append(entry)
        if self.on_add:
            self.on_add(entry)

    def add_tool_results(self, call_ids: list, contents: list) -> None:
        """tool 호출 순서와 1:1 대응하는 role:'tool' 결과 메시지들을 추가한다."""
        for cid, obs in zip(call_ids, contents):
            entry = {"role": "tool", "tool_call_id": cid,
                     "content": f"[도구 결과] →\n<untrusted>\n{obs}\n</untrusted>"}
            self.turns.append(entry)
            if self.on_add:
                self.on_add(entry)
        self.trim()
        self.repair()

    def strip_open_tool_round(self) -> None:
        """실행 중 Ctrl+C 로 끝나지 않은 라운드의 assistant(tool_calls)을 제거한다.
        다음 호출 때 서버 400을 만들지 않도록 dangling 토큰을 정리한다."""
        while self.turns and self.turns[-1].get("role") == "tool":
            self.turns.pop()
        if self.turns and self.turns[-1].get("role") == "assistant" \
                and self.turns[-1].get("tool_calls"):
            self.turns[-1].pop("tool_calls", None)

    def set_summary(self, text: str) -> None:
        self.summary = str(text or "").strip()[-self.summary_max_chars:]

    def messages(self) -> List[Dict[str, str]]:
        note = ""
        if self.summary:
            note += f"\n\n[이전 대화 요약]\n{self.summary}"
        if self._dropped:
            note += f"\n(컨텍스트 제한으로 이전 턴 {self._dropped}개 생략됨)"
        return [{"role": "system", "content": self.system + note}] + list(self.turns)

    def size(self) -> int:
        total = estimate(self.system) + estimate(self.summary)
        for m in self.turns:
            total += estimate(m.get("content", ""))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                total += estimate(fn.get("name", "")) + estimate(fn.get("arguments", ""))
        return total

    def repair(self) -> None:
        """assistant(tool_calls) ↔ role:tool 페어링 불변식 복구.

        컨텍스트 트리밍/세션 재개로 라운드가 중간에 잘리면 그 라운드 전체를
        버려 서버가 400 을 내지 않게 한다. 고아 role:tool 메시지는 제거한다.
        """
        kept: List[Dict] = []
        pending = None          # 미완결 라운드의 남은 tool_call_id 집합
        round_start = None      # kept 안의 해당 assistant 인덱스
        for t in self.turns:
            role = t.get("role")
            if role == "assistant" and t.get("tool_calls"):
                ids = [c.get("id") for c in t.get("tool_calls", [])
                       if isinstance(c, dict) and c.get("id")]
                if pending:
                    del kept[round_start:]
                    self._dropped += 1 + len(pending)
                    pending = None
                if ids:
                    kept.append(t)
                    round_start = len(kept) - 1
                    pending = {cid: True for cid in ids}
                else:
                    kept.append(t)
            elif role == "tool":
                cid = t.get("tool_call_id")
                if pending and cid in pending:
                    kept.append(t)
                    del pending[cid]
                else:
                    self._dropped += 1  # 고아 tool 결과
            else:
                if pending:
                    del kept[round_start:]
                    self._dropped += 1 + len(pending)
                    pending = None
                kept.append(t)
        if pending:
            del kept[round_start:]
            self._dropped += 1 + len(pending)
        self.turns = kept

    def load_turns(self, turns: List[Dict], dropped: int = 0) -> None:
        self.turns.extend(turns)
        self._dropped += max(0, int(dropped))
        self.repair()
        self.trim()

    def trim_to(self, budget: int) -> None:
        self.budget = max(1000, int(budget))
        self.trim()

    def compact(self, keep: int = 12, max_chars: Optional[int] = None,
                summarizer=None) -> str:
        """오래된 턴을 짧은 요약으로 접고 최근 턴만 남긴다. 도구 페어링은 repair로 맞춘다."""
        keep = max(4, int(keep))
        if len(self.turns) <= keep:
            return self.summary
        max_chars = int(max_chars or self.summary_max_chars)
        old = self.turns[:-keep]
        lines: List[str] = []
        for m in old:
            role = m.get("role")
            content = str(m.get("content") or "").strip()
            if role == "user" and not content.startswith("[도구 결과]"):
                lines.append(f"사용자: {_short(content)}")
            elif role == "assistant":
                if content:
                    lines.append(f"도움: {_short(content)}")
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or "tool"
                    args = _short(fn.get("arguments") or "", 160)
                    lines.append(f"도구호출: {name} {args}")
            elif role == "tool":
                lines.append(f"도구결과: {_short(content, 160)}")
        summary = "\n".join(lines[-120:])[-max_chars:]
        if summarizer and old:
            try:
                old_text = "\n".join(lines)
                llm_summary = str(summarizer(old_text) or "").strip()
                if llm_summary:
                    summary = llm_summary[-max_chars:]
            except Exception:
                log.debug("LLM 요약 실패 — 규칙 요약 사용", exc_info=True)
        if summary:
            self.set_summary((self.summary + "\n" + summary).strip() if self.summary else summary)
        self.turns = self.turns[-keep:]
        self._dropped += len(old)
        self.repair()
        log.debug("세션 요약: %d 턴 접음 (현재 %dtok)", len(old), self.size())
        return self.summary

    def _is_user_turn(self, m: Dict) -> bool:
        return m.get("role") == "user" and not str(m.get("content") or "").startswith("[도구 결과]")

    def trim(self) -> None:
        if self.size() <= self.budget:
            return
        while self.size() > self.budget and len(self.turns) > 4:
            cut = 1
            while cut < len(self.turns) - 4 and not self._is_user_turn(self.turns[cut]):
                cut += 1
            if cut == 1:
                cut = min(64, len(self.turns) - 4)
            del self.turns[:cut]
            self._dropped += cut
            log.debug("트리밍: %d 메시지 드롭 (현재 %dtok)", cut, self.size())
        self.repair()
