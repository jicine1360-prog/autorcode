"""PII 마스킹 — 클라우드 llm 으로 보내기 전 개인 정보를 자리표시로 바꾼다.

- mask_messages(msgs) → (마스킹된 메시지, mapping)
- restore(text, mapping) → 응답의 자리표시를 원본으로 복원 (클라우드에는 원본이 안 감)
- 로컬 ollama(127.0.0.1/localhost)나 AUTORCODE_PII_MASK=off 면 agent 가 적용하지 않는다.
"""
import re

PLACEHOLDER = "<PII:{}-{}>"

_PATTERNS = [
    # 주민등록번호 (110101-1234567 / 1101011234567)
    ("rrn", re.compile(r"\b\d{6}\s*-?\s*\d{7}\b")),
    # 한국 전화번호 (010-1234-5678 / 02-123-4567 / 070.... )
    ("phone", re.compile(r"\b0\d{1,2}-?\d{3,4}-?\d{4}\b")),
    # 이메일
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # 신용카드/계좌 모양 (1234-5678-9012-3456)
    ("card", re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b")),
]


def mask(text: str, mapping=None):
    """(마스킹 텍스트, {자리표시:원본}) — mapping 을 넘기면 같은 값은 같은 표시를 쓴다."""
    own = mapping is None
    if own:
        mapping = {}
    for kind, pat in _PATTERNS:
        def repl(m, kind=kind):
            val = m.group(0)
            for ph, prev in mapping.items():
                if prev == val:
                    return ph
            ph = PLACEHOLDER.format(kind, len([k for k in mapping if k.startswith(f"<PII:{kind}-")]) + 1)
            mapping[ph] = val
            return ph
        text = pat.sub(repl, text)
    return text, mapping


def mask_messages(messages):
    """메시지 리스트의 content 문자열을 마스킹. tool/user/assistant 전부."""
    mapping = {}
    out = []
    for m in messages:
        mm = dict(m)
        content = mm.get("content")
        if isinstance(content, str) and content:
            masked, _ = mask(content, mapping)
            mm["content"] = masked
        out.append(mm)
    return out, mapping


def restore(text: str, mapping) -> str:
    if not text or not mapping:
        return text
    for ph, val in mapping.items():
        text = text.replace(ph, val)
    return text


def is_local_base(base_url: str) -> bool:
    b = (base_url or "").lower()
    return ("127.0.0.1" in b or "localhost" in b or b == "")


def should_mask(base_url: str, cfg_mask_env: str = "") -> bool:
    env = cfg_mask_env.lower()
    if env == "off":
        return False
    if env == "on":
        return True
    return not is_local_base(base_url)
