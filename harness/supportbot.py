"""autorcode 관리봇 — 사용자 문의 자동 응대 (전화 없음, 사람 최소 개입).

동작:
  1. 문의 채널(Open WebUI 메시지/이메일 파이프) 입력을 받아
  2. 로컬 LLM(라우터)로 질문 의도를 분류 → FAQ 자동답 / 고급 답변 작성
  3. 답은 이메일로 발송(jicine1360@gmail.com 수신자) 또는 Open WebUI 채팅으로 응답
  4. 사람 개입이 필요한 경우만 관리자에게 한 번 알림(이메일) 후 대기

원칙:
- 전화번호는 제공하지 않는다 (지원은 관리봇 + 이메일만).
- 개인정보는 서버 밖으로 안 나감 — 분류/답변 모두 로컬 모델 사용.
- 긴급/불만/유료 요청만 human-escalation으로 푸시.
"""
import json
import logging
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from dataclasses import dataclass

log = logging.getLogger("agent.supportbot")

_ADMIN_EMAIL = os.getenv("SUPPORT_BOT_ADMIN_EMAIL", "jicine1360@gmail.com")
_SMTP_HOST = os.getenv("SUPPORT_BOT_SMTP_HOST", "")
_SMTP_PORT = int(os.getenv("SUPPORT_BOT_SMTP_PORT", "587"))
_SMTP_USER = os.getenv("SUPPORT_BOT_SMTP_USER", "")
_SMTP_PASS = os.getenv("SUPPORT_BOT_SMTP_PASS", "")
_FROM_EMAIL = os.getenv("SUPPORT_BOT_FROM_EMAIL", _SMTP_USER)

FAQ = [
    {"keys": ["접속", "로그인", "주소", "url", "주소"], "answer":
        "서비스는 브라우저로 접속합니다. 문의 주신 내용에 맞는 안내는 "
        "studio.hostingtower.cloud(안내서 문서)를 참고하세요."},
    {"keys": ["비밀번호", "암호", "password", "로그인 안 되"], "answer":
        "로그인 정보는 안내서(studio.hostingtower.cloud 사용설명서 PDF)에 있습니다. "
        "비밀번호 재설정을 원하시면 이메일로 재설정 요청이라고 보내주세요."},
    {"keys": ["가격", "요금", "비용", "비싸", "구독", "유료", "무료", "크레딧"], "answer":
        "현재 서비스 요금: 크레딧/포인트제 이용 중입니다. 자세한 요금표는 "
        "jicine1360@gmail.com 으로 문의하시면 자동화된 안내를 드립니다."},
    {"keys": ["전화", "번호", "콜", "통화", "상담사"], "answer":
        "죄송합니다만 전화 지원은 제공하지 않고, 모든 문의는 이메일 "
        "jicine1360@gmail.com 으로 처리됩니다. 보통 24시간 이내 자동/수동 답변을 드립니다."},
    {"keys": ["엑셀", "xlsx", "엑셀파일", "스프레드시트"], "answer":
        "엑셀(.xlsx) 요약·정리가 가능합니다. 파일을 업로드하거나 서버 경로를 알려주세요. "
        "예: '/data/report.xlsx' 요약해서 엑셀로."},
    {"keys": ["pdf", "PDF"], "answer":
        "PDF 문서를 텍스트로 변환·요약할 수 있습니다. 서버 경로를 알려주세요."},
    {"keys": ["안녕", "hello", "시작", "대표"], "answer":
        "안녕하세요. autorcode 지원봇입니다. 서버 파일 조회, 웹 검색, 엑셀/PDF 요약 등 "
        "가능합니다. 무엇을 도와드릴까요?"},
]

_ESCALATE = ["불만", "환불", "에러", "장애", "안 되", "고장", "버그", "데이터 삭제", "유출", "보안", "해킹"]

# 야간(younger) 전용 고난도 LLM — 별도 엔드포인트로 라우팅 (fast보다 강한 모델)
_NIGHT_BASE_URL = os.getenv("SUPPORT_NIGHT_BASE_URL",
                            "http://100.110.82.116:8086/v1")
_NIGHT_MODEL = os.getenv("SUPPORT_NIGHT_MODEL", "")  # 비면 서버 기본 모델 사용
_NIGHT_API_KEY = os.getenv("SUPPORT_NIGHT_API_KEY", "")


@dataclass
class Ticket:
    kind: str  # faq | llm | escalate
    answer: str
    reason: str


def _classify(msg: str) -> Ticket:
    for f in FAQ:
        if any(k.lower() in msg.lower() for k in f["keys"]):
            return Ticket("faq", f["answer"], f"FAQ 매치({f['keys'][0]})")
    if any(k.lower() in msg.lower() for k in _ESCALATE):
        return Ticket("escalate", "담당자가 검토 후 답변드리겠습니다.",
                      f"에스컬레이션 키워드")
    return Ticket("llm", "", "로컬 LLM 답변")


def _local_answer(msg: str) -> str:
    """로컬 모델로 간단한 답변 생성. 실패 시 기본 안내."""
    try:
        from .llm import OpenAICompatibleLLM
        model = OpenAICompatibleLLM(
            os.getenv("AGENT_BASE_URL", "http://127.0.0.1:11434/v1"),
            os.getenv("AGENT_API_KEY", "ollama"),
            timeout=int(os.getenv("AGENT_API_TIMEOUT", "120")),
            retries=int(os.getenv("AGENT_API_RETRIES", "2")),
            temperature=0.2,
            max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "700")),
        )
        messages = [
            {"role": "system",
             "content": "넌 지원봇. 짧고 정확하게. 전화 지원은 없다고 안내."},
            {"role": "user", "content": msg},
        ]
        resp = model.chat(messages, model=os.getenv("AGENT_MODEL_FAST", "phi4:latest"))
        out = resp.strip()[:600]
        return out or "지원봇이 답변을 준비하지 못했습니다. 이메일로 다시 문의해 주세요."
    except Exception as e:
        log.warning("LLM 답변 실패 → 기본 안내: %s", e)
        return ("문의 감사합니다. 구체적으로는 이메일 jicine1360@gmail.com 으로 "
                "문의해 주시면 자세히 답변드립니다. (현재 로컬 응대 일시 중단)")


def _hard_answer(msg: str) -> str:
    """고난도(야간/energy) 문의 — younger의 nightshift(대형 모델)로 답변.

    fast(phi4)로는 부족한 긴 문의·복합 질문에 사용. 응답이 매우 느리므로
    (프리필 + 디코드 수십 초~수 분) 실패 시 fast 답변으로 폴백한다.
    """
    try:
        from .llm import OpenAICompatibleLLM
        model = OpenAICompatibleLLM(
            _NIGHT_BASE_URL, _NIGHT_API_KEY,
            timeout=int(os.getenv("SUPPORT_NIGHT_TIMEOUT", "600")),
            retries=1,
            temperature=0.2,
            max_tokens=int(os.getenv("SUPPORT_NIGHT_MAX_TOKENS", "1200")),
        )
        messages = [
            {"role": "system",
             "content": "넌 autorcode 지원 어시스턴트. 신중하고 정확하게, "
                        "전화 지원은 없다고 안내하고, 모르면 솔직히 모른다."},
            {"role": "user", "content": msg},
        ]
        resp = model.chat(messages, model=_NIGHT_MODEL or "nightshift")
        out = resp.strip()[:1200]
        return out or "지원봇이 답변을 준비하지 못했습니다."
    except Exception as e:
        log.warning("nightshift(hard) 답변 실패 → fast 폴백: %s", e)
        return _local_answer(msg)


def handle_inquiry(msg: str, channel: str = "openwebui") -> dict:
    ticket = _classify(msg)
    if ticket.kind == "llm":
        ticket.answer = _local_answer(msg)
    if ticket.kind == "escalate":
        ticket.answer = _hard_answer(msg)
    if ticket.kind in ("llm", "escalate"):
        _notify_admin(msg, ticket)
    log.info("처리: kind=%s reason=%s channel=%s", ticket.kind, ticket.reason, channel)
    return {"kind": ticket.kind, "reply": ticket.answer,
            "escalated": ticket.kind == "escalate"}


def _notify_admin(msg: str, ticket: Ticket) -> None:
    if not (_SMTP_HOST and _SMTP_USER and _SMTP_PASS):
        log.info("SMTP 미설정 → 알림 생략 (관리자 확인 필요: %s)", _ADMIN_EMAIL)
        return
    subject = f"[autorcode 지원] {ticket.kind.upper()} 문의 접수"
    body = f"의도: {ticket.reason}\n\n문의 내용:\n{msg[:800]}"
    _send_mail(_ADMIN_EMAIL, subject, body)


def send_reply(to: str, ticket: Ticket) -> bool:
    if ticket.kind == "escalate":
        body = (f"{ticket.answer}\n\n"
                f"(내용은 담당자 확인 중입니다. 보안·데이터 이슈는 이메일 원문으로 "
                f"접수해 주시면 감사하겠습니다.)")
    else:
        body = ticket.answer
    return _send_mail(to, "[autorcode 지원봇] 자동 답변", body)


def _send_mail(to: str, subject: str, body: str) -> bool:
    if not (_SMTP_HOST and _SMTP_USER and _SMTP_PASS):
        log.info("SMTP 미설정 → 발송 생략: %s", subject)
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = _FROM_EMAIL
    msg["To"] = to
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as s:
            s.starttls()
            s.login(_SMTP_USER, _SMTP_PASS)
            s.send_message(msg)
        log.info("이메일 발송: %s → %s", subject, to)
        return True
    except Exception as e:
        log.error("이메일 발송 실패: %s", e)
        return False


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
    else:
        msg = input("문의 입력: ").strip()
    if not msg:
        return
    res = handle_inquiry(msg)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()