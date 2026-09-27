"""원격 승인 게이트 — 휴대폰에서 'y/N'을 대신 눌러주는 confirmer.

왜 이게 필요한가:
  agent.py 는 `confirmer=ask if sys.stdin.isatty() else None` 이고, bridge.py 는
  `confirmer=lambda _q: False` 다. 즉 TTY 없이는 승인 가능한 작업이 하나도 없다
  (fail-closed 라서 틀리진 않지만, systemd 로 뜨면 무기력하다). 지금까지 그 공백을
  메우던 수단은 pcgate(포트 8791 인바운드 HTTP)였다 — 여는 포트를 만들지 않고
  같은 문제를 푸는 것이 이 모듈이다.

설계 원칙 (이 셋이 이 파일의 존재 이유다):
  1. 닫히면 안 된다 — 설정 없음 / 토큰 파일이 남에게 보임 / 전송 실패 / 시간 초과
     전부 False(거부)로 끝난다. 승인 게이트는 "기본 허용"으로 넘어가면 안 된다.
  2. 한 번만 쓴다 — nonce는 consume 시점에 삭제된다. 화면에 남아있는 버튼을
     두 번 눌러도 한 번만 실행된다.
  3. 요청자가 눌러야 한다 — 응답자의 user id가 허용 목록에 있어야 하고, 보낸
     chat과 일치해야 한다. 그룹에 초대된 사람이 대신 승인할 수 없다.

  표준 라이브러리만 쓴다(pip 설치 금지라는 프로젝트 제약).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import stat
import threading
import urllib.error
import urllib.request
from typing import Callable, Optional

from .telegram import CONFIG_PATH

log = logging.getLogger("agent.approve")

API_BASE = os.getenv("AUTORCODE_TELEGRAM_API", "https://api.telegram.org")

# 승인을 기다리는 시간. 이 시간이 지나면 거부로 처리한다. 오래된 승인은 위험하다 —
# 사용자가 폰을 들고 산책하다 30분 뒤에 approving 하는 일은 사고가 된다.
DEFAULT_TIMEOUT = 180


def _resolve_timeout(data: dict) -> int:
    """승인 대기시간 결정. 우선순위: 환경변수 > 설정 파일 > 기본값.

    환경변수가 이긴다는 게 의도다. systemd 의 Environment= 처럼 특정 배포에서만
    다른 값이 필요할 때 설정 파일을 고칠 필요 없이 배포 단위로 덮어쓸 수 있다.
    파일이 이기도록 두면, Environment= 로 90초를 지정한 서비스가 조용히 파일의
    180초를 쓰게 되어 "설정은 바꿨는데 왜 안 바뀌지" 하는 일이 된다. 실제로
    그랬다 — data.get("approveTimeout", DEFAULT_TIMEOUT) 은 파일이 항상 이겼다.
    """
    env = os.getenv("AUTORCODE_APPROVE_TIMEOUT", "").strip()
    if env:
        return int(env)
    return int(data.get("approveTimeout", DEFAULT_TIMEOUT))

# 텔레그램 sendMessage 본문 상한(4096). 여유를 둔다.
_TEXT_LIMIT = 3500

_BTN_YES = "승인"
_BTN_NO = "거부"


class GateUnavailable(RuntimeError):
    """승인 게이트를 쓸 수 없는 상태. 구성 문제이므로 조용히 넘어가지 않는다."""


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

def load_config(path: str = CONFIG_PATH) -> dict:
    """승인 게이트 설정을 읽는다. 문제가 있으면 예외 — 호출측이 거부로 처리한다."""
    if not os.path.exists(path):
        raise GateUnavailable(f"설정 파일 없음: {path}")

    # 토큰 파일이 그룹/전체에게 읽히면, 그 파일을 읽을 수 있는 사람이 내 에이전트의
    # 명령 승인을 넘겨받을 수 있다. 리포트는 노출되어도 디미지가 없지만 승인은 다르다.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise GateUnavailable(
            f"권한이 느슨한 설정 파일: {path} (모드 {mode:o}). "
            f"'chmod 600 {path}' 를 실행할 것"
        )

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    token = str(data.get("token", "") or "").strip()
    if not token:
        raise GateUnavailable("bot 토큰이 비어 있음")

    # 허용 목록이 없으면 예전 설정(chat 하나)으로부터 승격. 'chat' 하나짜리는
    # 목록 크기 1과 같으므로 마이그레이션이 안전하다.
    raw_ids = data.get("allowedChatIds")
    if raw_ids is None:
        chat = data.get("chat")
        allowed = [int(chat)] if str(chat or "").strip() else []
    else:
        allowed = [int(x) for x in raw_ids]

    if not allowed:
        raise GateUnavailable(
            "허용 chat 목록이 비어 있음 (allowedChatIds). "
            "비어 있는 상태로 승인 게이트를 여는 것은 아무에게나 명령 승인을 "
            "주는 것과 같으므로 거부한다"
        )

    return {
        "token": token,
        "allowed": allowed,
        "timeout": _resolve_timeout(data),
    }


# ---------------------------------------------------------------------------
# 대기 중인 승인
# ---------------------------------------------------------------------------

class _Pending:
    __slots__ = ("chat_id", "message_id", "event", "verdict")

    def __init__(self, chat_id: int, message_id: int = 0):
        self.chat_id = chat_id
        self.message_id = message_id
        self.event = threading.Event()
        self.verdict: Optional[bool] = None


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------

class ApprovalGate:
    """`confirmer` 로 그대로 넘길 수 있는 callable.

    질문 하나를 보내고, 응답이 오거나 시간 초과할 때까지 블로킹한 bool 을 돌려준다.
    """

    def __init__(
        self,
        token: str,
        allowed: list[int],
        timeout: int = DEFAULT_TIMEOUT,
        api_base: str = API_BASE,
        opener: Optional[Callable] = None,
    ):
        self._token = token
        self._allowed = set(allowed)
        self._timeout = timeout
        self._api = api_base.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset: Optional[int] = None
        self._target = sorted(self._allowed)[0]

    # -- Telegram API ------------------------------------------------------

    def _call(self, method: str, payload: dict):
        url = f"{self._api}/bot{self._token}/{method}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self._opener(req, timeout=40) as r:
            body = json.loads(r.read() or b"{}")
        if not body.get("ok"):
            desc = body.get("description", "실패")
            code = int(body.get("error_code", 0) or 0)
            # 409/401 은 재시도해도 회복되지 않는다. 문자열에 코드를 섞어 던져서
            # 호출측이 구분할 수 있게 한다.
            raise RuntimeError(f"{code}:{desc}")
        return body.get("result")

    # -- confirmer ---------------------------------------------------------

    def __call__(self, question: str) -> bool:
        """승인 질문 하나. True=승인, False=거부(시간 초과 포함)."""
        if self._stop.is_set():
            return False

        nonce = secrets.token_hex(12)
        pending = _Pending(self._target)
        with self._lock:
            self._pending[nonce] = pending

        try:
            text = f"[승인 요청]\n{question}\n\n{self._timeout}초 안에 답하지 않으면 거부됩니다."
            result = self._call("sendMessage", {
                "chat_id": pending.chat_id,
                "text": text[:_TEXT_LIMIT],
                "reply_markup": {"inline_keyboard": [[
                    {"text": _BTN_YES, "callback_data": f"ag:y:{nonce}"},
                    {"text": _BTN_NO, "callback_data": f"ag:n:{nonce}"},
                ]]},
            })
            pending.message_id = int((result or {}).get("message_id", 0) or 0)
        except Exception as e:  # 전송 실패 = 승인 못 한 것 = 거부
            log.warning("승인 요청 전송 실패: %s", e)
            with self._lock:
                self._pending.pop(nonce, None)
            return False

        approved = pending.event.wait(self._timeout)
        if not approved:
            # 시간 초과. 버튼을 회수해 나중에 눌려도 실행되지 않게 하고, 사용자에게
            # 왜 안 됐는지 남긴다.
            self._expire(nonce, pending)
            with self._lock:
                self._pending.pop(nonce, None)
            log.info("승인 시간 초과 (%.0fs)", self._timeout)
            return False

        with self._lock:
            self._pending.pop(nonce, None)
        return pending.verdict is True

    def _expire(self, nonce: str, pending: _Pending) -> None:
        if pending.message_id <= 0:
            return
        try:
            self._call("editMessageText", {
                "chat_id": pending.chat_id,
                "message_id": pending.message_id,
                "text": "[만료] 시간 안에 응답이 없어 요청을 거부했습니다.",
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as e:
            log.debug("만료 메시지 편집 실패(무시): %s", e)

    # -- 폴러 --------------------------------------------------------------

    def start(self) -> None:
        """수신 폴러를 백그라운드로 띄운다. 승인 전에도 띄워 두는 게 좋다."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._poll, name="tg-approve", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll(self) -> None:
        log.debug("승인 폴러 시작 (offset=%s)", self._offset)
        while not self._stop.is_set():
            try:
                # callback_query 만 받는다. 사용자가 보낸 일반 메시지를 소비해 버리면
                # 나중에 리포트/다른 기능이 그 메시지를 못 본다. 버튼 탭만 받으면
                # 승인에 필요한 것은 다 처리되고 일반 메시지는 큐에 그대로 남는다.
                payload = {"timeout": 30, "allowed_updates": ["callback_query"]}
                if self._offset is not None:
                    payload["offset"] = self._offset
                updates = self._call("getUpdates", payload) or []

                for u in updates:
                    uid = int(u.get("update_id", 0))
                    self._offset = uid + 1 if self._offset is None else max(self._offset, uid + 1)
                    try:
                        self._dispatch(u)
                    except Exception:
                        log.exception("업데이트 처리 실패")

                if not updates:
                    # 빈 응답에 40초를 소모하므로 기다리지 않는다(장시간 폴링이라
                    # offset 충돌 창을 좁게 유지하는 편이 낫다).
                    self._stop.wait(0.5)
            except RuntimeError as e:
                code = str(e).split(":", 1)[0]
                if code == "409":
                    log.error("같은 토큰으로 다른 폴러가 이미 동작 중. 승인은 그쪽으로 갑니다.")
                    return
                if code == "401":
                    log.error("봇 토큰이 거부됨. 게이트를 끈다.")
                    return
                log.warning("폴링 실패: %s", e)
                self._stop.wait(3)
            except Exception as e:
                log.warning("폴링 오류: %s", e)
                self._stop.wait(3)
        log.debug("승인 폴러 종료")

    def _dispatch(self, update: dict) -> None:
        q = update.get("callback_query")
        if not q:
            return
        cb_id = str(q.get("id", ""))
        data = str(q.get("data", "") or "")
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "ag":
            self._answer(cb_id, "무시된 버튼")
            return
        _, verdict, nonce = parts
        approve = verdict == "y"

        user_id = int((q.get("from") or {}).get("id", 0) or 0)
        chat_id = int(((q.get("message") or {}).get("chat") or {}).get("id", 0) or 0)

        with self._lock:
            pending = self._pending.get(nonce)

        if pending is None:
            # 이미 처리됐거나 타임아웃으로 회수된 요청.
            self._answer(cb_id, "이미 처리된 요청입니다")
            return
        if user_id not in self._allowed:
            self._answer(cb_id, "승인 권한이 없습니다")
            return
        if chat_id != pending.chat_id:
            self._answer(cb_id, "다른 곳의 요청입니다")
            return

        pending.verdict = approve
        pending.event.set()
        self._answer(cb_id, "실행합니다" if approve else "거부했습니다")

    def _answer(self, cb_id: str, text: str) -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})
        except Exception as e:
            log.debug("콜백 응답 실패(무시): %s", e)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def build_from_config(path: str = CONFIG_PATH) -> Optional[ApprovalGate]:
    """설정에서 게이트를 만든다. 구성 문제가 있으면 None (호출측이 거부로 처리).

    여기서 예외를 던지지 않는 이유: 게이트를 못 만든 것과 승인을 거부한 것은
    관점에서 같기 때문이다(둘 다 '실행하지 않음'). 경고는 로그로 남긴다.
    """
    # 설정 파일이 없는 것과 설정이 잘못된 것은 다른 사건이다. 없는 쪽은
    # "텔레그램 승인을 쓰지 않는다"는 정상 설정이라 WARNING 으로 올리면 안 된다 —
    # 대부분의 사용자에게는 늘 그렇고, 경고가 stderr 로 새어 --quiet 도 깨뜨린다.
    # 있으면 잘못된 것이므로 그때는 WARNING 을 유지한다.
    if not os.path.exists(path):
        log.info("승인 게이트 미사용: 텔레그램 설정 없음 (%s)", path)
        return None

    try:
        cfg = load_config(path)
    except GateUnavailable as e:
        log.warning("승인 게이트 사용 안 함: %s", e)
        return None
    except Exception:
        log.exception("승인 게이트 설정 읽기 실패")
        return None

    gate = ApprovalGate(cfg["token"], cfg["allowed"], timeout=cfg["timeout"])
    gate.start()
    # 이 경로의 의미가 크다: TTY 없는 실행은 예전엔 승인 필요한 작업을 전부 거부했다.
    # 이제는 폰을 기다린다. systemd TimeoutSec 보다 approveTimeout 이 길면 잡이 죽는다.
    log.info(
        "승인 게이트 활성: 허용 %d명, 타임아웃 %ds (대기 중)",
        len(cfg["allowed"]), cfg["timeout"],
    )
    return gate
