"""모델 라우터 — 요청 난이도를启发式으로 점수화해 fast/smart 티어를 선택.

프로덕션 확장 훅:
- keyword 점수 대신 임베딩/의도분류기 사용 가능
- LLM 저지판(judge)로 라우팅 결정을 검증하는 2차 라우트 추가 가능
"""
import logging

log = logging.getLogger("agent.router")

_SMART = [
    "분석", "리팩", "디버그", "버그", "설계", "아키텍처", "구현", "최적화",
    "마이그레이션", "성능", "테스트", "여러 파일", "원인", "왜", "구조",
    "refactor", "debug", "analyz", "implement", "design", "optimiz",
    "architecture", "migrat", "investigat",
]
_FAST = [
    "ls", "목록", "리스트", "실행", "읽어", "읽기", "생성", "만들", "쓰",
    "작성", "삭제", "hello", "인사", "시간", "날짜", "touch", "echo", "cat",
    "show", "run",
]


def route(task: str) -> tuple:
    t = task.lower()
    smart = sum(1 for k in _SMART if k in t)
    fast = sum(1 for k in _FAST if k in t)
    if smart > fast:
        tier = "smart"
    elif fast > smart:
        tier = "fast"
    else:
        tier = "fast"  # 애매하면 저비용 기본
    reason = f"smart={smart} fast={fast} → {tier}"
    log.info("라우팅: %s", reason)
    return tier, reason
