"""토큰 추정 — tiktoken이 있으면 정확 계산, 없으면 CJK/라틴 휴리스틱.

cl100k 기준 한자 ≈ 0.7~1.0 tok, 영문 ≈ 0.25 tok/char.
추정치는 컨텍스트 예산/메트릭 용도이므로 보수적으로 잡는다.
"""
try:
    import tiktoken  # noqa: F401
    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _enc = None

_KANA = (0x3040, 0x30FF)
_HANGUL = (0x3131, 0xD7FF)
_CJK = (0x4E00, 0x9FFF)


def _cjk_count(text: str) -> int:
    return sum(1 for c in text
               if _KANA[0] <= ord(c) <= _KANA[1]
               or _HANGUL[0] <= ord(c) <= _HANGUL[1]
               or _CJK[0] <= ord(c) <= _CJK[1])


def count(text: str) -> int:
    if _enc is not None:
        return len(_enc.encode(text))
    cjk = _cjk_count(text)
    return int(cjk + (len(text) - cjk) / 3.5) + 1
