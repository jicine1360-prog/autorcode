"""텔레그램 폰 비서 — 승인 게이트와 명령 수신을 하나의 폴러로 합친다.

telegram getUpdates 는 토큰당 소비자(폴러)가 한 명이어야 한다(409 충돌).
approve.py 의 ApprovalGate 는 callback_query 만 폴링하므로, 이 모듈이 두 일을
대신한다:

  - callback_query(승인 버튼 탭) → gate.feed() 로 넘겨 기존 검증 그대로 사용
  - message(사용자 명령)      → Agent 를 실행하고 결과를 같은 chat 으로 회신

같은 봇 토큰으로 별도 폴러를 띄우지 않는 한 어디서 만든 Agent 든 승인 요청은
이 봇의 폴러가 받아 실행된다. 표준 라이브러리만 쓴다(pip 설치 금지 제약).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request
from typing import Callable, Optional

from .agent_core import Agent
from .approve import ApprovalGate, DEFAULT_TIMEOUT, GateUnavailable, load_config
from .config import Config, load as load_config_env
from .progress import Progress
from .telegram import CONFIG_PATH, collect_status

log = logging.getLogger("agent.bot")

API_BASE = os.getenv("AUTORCODE_TELEGRAM_API", "https://api.telegram.org")
_TEXT_LIMIT = 3900
# 폰 비서 전용 봇 설정. telegram.json(리포트/구승인 게이트 공용 토큰)은 openclaw
# 등의 다른 폴러와 공유되므로, 봇은 별도 @BotFather 발급 토큰을 기본으로 쓴다.
BOT_CONFIG = os.path.expanduser("~/.autorcode/bot.json")
HELP = (
    "autorcode 폰 비서에 오신 것을 환영합니다 🦜\n\n"
    "명령이 아닌 텍스트는 전부 작업 지시로 실행됩니다.\n"
    "/status  서버 상태 요약\n"
    "/report  일일 리포트 전송\n"
    "/help    이 도움말\n\n"
    "민감한 명령은 여기서 승인/거부 버튼을 눌러 결정하고,\n"
    "거부하면 실행되지 않습니다."
)


def _default_workspace() -> str:
    path = os.path.expanduser(os.getenv("AUTORCODE_BOT_WORKSPACE", "~/autorcode-bot"))
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


class Bot:
    """텔레그램 명령 수신 + 승인 버튼 처리를 한 폴러로 처리하는 비서."""

    def __init__(
        self,
        token: str,
        allowed: list[int],
        gate: Optional[ApprovalGate] = None,
        api_base: str = API_BASE,
        opener: Optional[Callable] = None,
        agent_factory: Optional[Callable[[], Agent]] = None,
        workspace: Optional[str] = None,
    ):
        self._token = token
        self._allowed = set(allowed)
        self._api = api_base.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._gate = gate or ApprovalGate(token, list(self._allowed))
        self._agent_factory = agent_factory
        self._workspace = workspace or _default_workspace()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset: Optional[int] = None
        self._busy = False
        self._lock = threading.Lock()

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
            raise RuntimeError(f"{code}:{desc}")
        return body.get("result")

    def send(self, chat_id: int, text: str) -> bool:
        """회신. 실패해도 폴러를 죽이지 않는다(가급적 로그만 남긴다)."""
        try:
            self._call("sendMessage", {
                "chat_id": chat_id,
                "text": text[:_TEXT_LIMIT],
                "disable_web_page_preview": True,
            })
            return True
        except Exception as e:
            log.warning("전송 실패 (chat %s): %s", chat_id, e)
            return False

    # -- 수명주기 ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._poll, name="tg-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- 폴러 --------------------------------------------------------------

    def _poll(self) -> None:
        log.info("텔레그램 폴러 시작 (허용 %d명)", len(self._allowed))
        while not self._stop.is_set():
            try:
                payload = {"timeout": 30,
                           "allowed_updates": ["message", "edited_message", "callback_query"]}
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
                    self._stop.wait(0.5)
            except RuntimeError as e:
                code = str(e).split(":", 1)[0]
                if code in ("409", "401"):
                    log.error("폴러 종료(회복 불가): %s", e)
                    return
                log.warning("폴링 실패: %s", e)
                self._stop.wait(3)
            except Exception as e:
                log.warning("폴링 오류: %s", e)
                self._stop.wait(3)
        log.info("텔레그램 폴러 종료")

    # -- 분배 --------------------------------------------------------------

    def _dispatch(self, update: dict) -> None:
        if update.get("callback_query"):
            if self._gate is not None:
                self._gate.feed(update)
            return

        m = update.get("message") or update.get("edited_message")
        if not m:
            return
        chat_id = int(((m.get("chat") or {}).get("id") or 0))
        text = str(m.get("text") or "").strip()
        if not text or chat_id <= 0:
            return
        if chat_id not in self._allowed:
            log.warning("허용되지 않은 chat(%s) 의 메시지 무시", chat_id)
            return
        self._handle(chat_id, text)

    # -- 명령 --------------------------------------------------------------

    def _handle(self, chat_id: int, text: str) -> None:
        cmd = text.split(maxsplit=1)[0]
        if cmd in ("/start", "/help"):
            self.send(chat_id, HELP)
            return
        if cmd in ("/status", "/report"):
            self.send(chat_id, collect_status())
            return

        with self._lock:
            if self._busy:
                self.send(chat_id, "이미 작업 중입니다. 하나씩만 받을 수 있어요 — 끝나면 다시 보내주세요.")
                return
            self._busy = True
        threading.Thread(target=self._work, args=(chat_id, text), daemon=True, name="tg-task").start()

    # -- 작업 --------------------------------------------------------------

    def _work(self, chat_id: int, text: str) -> None:
        try:
            if self._gate is not None:
                self._gate.current_chat = chat_id
            agent = self._agent_factory() if self._agent_factory else self._default_agent()
            self.send(chat_id, f"작업 시작 🛠 ({len(text)}자)\n승인이 필요하면 버튼이 올라옵니다.")
            result = agent.run(text)
            if self._gate is not None:
                self._gate.current_chat = None
            self.send(chat_id, result)
        except Exception as e:
            log.exception("작업 실패 (chat %s)", chat_id)
            if self._gate is not None:
                self._gate.current_chat = None
            self.send(chat_id, f"작업 중 오류: {str(e)[:_TEXT_LIMIT]}")
        finally:
            with self._lock:
                self._busy = False

    def _default_agent(self) -> Agent:
        """전화 작업 기본 에이전트.

        전용 workspace + workspace 기준 세션 파일로 다음 작업 때 이전 파악 내용을
        이어받는다. LLM 백엔드는 AGENT_PROVIDER=ollama 기본(설정이 있으면 그대로).
        """
        os.environ.setdefault("AGENT_PROVIDER", "ollama")
        cfg: Config = load_config_env()
        cfg.workspace_root = self._workspace
        if not cfg.session_file:
            h = hashlib.md5(self._workspace.encode()).hexdigest()[:10]
            cfg.session_file = os.path.expanduser(f"~/.autorcode/session_{h}.jsonl")
            os.makedirs(os.path.dirname(cfg.session_file), exist_ok=True)
        cfg.show_steps = False  # stderr 로 나가지 않게 (폰에서는 먼저 실행 결과만)
        return Agent(cfg, confirmer=self._gate, progress=Progress(enabled=False))


def _env_token() -> Optional[str]:
    """환경변수 봇 토큰. AGENTUPBOT_TOKEN 또는 AUTORCODE_BOT_TOKEN.

    토큰을 git 에 두지 않고(공개 repo) env/EnvironmentFile 로 넘기는 흐름.
    """
    return (os.environ.get("AGENTUPBOT_TOKEN")
            or os.environ.get("AUTORCODE_BOT_TOKEN") or "").strip() or None


def _env_chats() -> Optional[list[int]]:
    """환경변수 허용 chat 목록. 단일 또는 쉼표/공백 구분 여러 명(가족).

    AGENTUPBOT_CHATS 를 우선하고, 없으면 AGENTUPBOT_CHAT 을 쓴다.
    """
    raw = (os.environ.get("AGENTUPBOT_CHATS")
           or os.environ.get("AGENTUPBOT_CHAT") or "").strip()
    chats: list[int] = []
    for part in raw.replace(",", " ").split():
        try:
            chats.append(int(part))
        except ValueError:
            log.warning("무시된 chat id: %r (숫자 아님)", part)
    return chats or None


def build_bot(path: Optional[str] = None, **kw) -> Optional[Bot]:
    """설정에서 봇을 만든다. 구성 문제가 있으면 None(fail-closed).

    우선순위:
      1. env AGENTUPBOT_TOKEN(/AUTORCODE_BOT_TOKEN) — 토큰을 git/파일 대신
         환경변수(EnvironmentFile 등)로 넘기는 흐름. chat 은 AGENTUPBOT_CHAT,
         없으면 기존 telegram.json 의 chat 을 재사용.
      2. ~/.autorcode/bot.json (전용 토큰)
      3. 예전 호환 telegram.json — 단, 그 토큰을 openclaw 등이 폴링 중이면
         409 로 포기한다(사용자에게 새 봇을 만들라고 안내).

    게이트가 직접 폴링해서는 안 되므로 build_from_config 를 쓰지 않고,
    로딩은 load_config 로, 게이트는 폴링 없이 손수 만든다. 폴러는 Bot 이 소유한다.
    """
    token = _env_token()
    if token is not None:
        chats = _env_chats()
        if not chats:
            # telegram.json 은 같은 사람의 chat 목록이 담겨 있을 수 있다 → 재사용
            try:
                chats = load_config(CONFIG_PATH)["allowed"]
            except Exception:
                chats = []
        if not chats:
            log.warning("AGENTUPBOT_TOKEN 은 있지만 chat id 를 모릅니다 — "
                        "AGENTUPBOT_CHATS(또는 CHAT) 를 설정하세요")
            return None
        gate = ApprovalGate(token, chats, timeout=DEFAULT_TIMEOUT)
        log.info("봇 토큰: 환경변수 (AGENTUPBOT_TOKEN, 허용 %d명)", len(chats))
        return Bot(token, chats, gate=gate, **kw)

    if path is None:
        path = BOT_CONFIG if os.path.exists(BOT_CONFIG) else CONFIG_PATH
    if not os.path.exists(path):
        log.info("폰 비서 미사용: 텔레그램 설정 없음 (%s)", path)
        return None
    try:
        cfg = load_config(path)
    except GateUnavailable as e:
        log.warning("폰 비서 사용 안 함: %s", e)
        return None
    except Exception:
        log.exception("폰 비서 설정 읽기 실패")
        return None

    gate = ApprovalGate(cfg["token"], cfg["allowed"], timeout=cfg["timeout"])
    return Bot(cfg["token"], cfg["allowed"], gate=gate, **kw)