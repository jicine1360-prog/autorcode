"""원격 승인 게이트 테스트.

이 게이트는 '화면을 안 보고 스크립트가 명령을 실행하게 만드는' 경로다. 그래서
성공 케이스보다 실패 케이스를 더 많이 검증한다 — 게이트는 닫혀야만 안전하다.
"""
import json
import logging
import os
import stat
import tempfile
import threading
import time
import unittest

from harness import approve


class FakeApi:
    """Telegram API 대역. getUpdates 는 등록된 업데이트를 한 번만 돌려준다."""

    def __init__(self):
        self.calls = []
        self.answers = []
        self.queue = []
        self.fail_send = False
        self.error_code = None

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        payload = json.loads(req.data or b"{}")
        self.calls.append((method, payload))
        if self.error_code:
            return _Resp({"ok": False, "error_code": self.error_code,
                          "description": "stub error"})
        if method == "answerCallbackQuery":
            self.answers.append(payload.get("text", ""))
        if method == "getUpdates":
            out, self.queue = self.queue, []
            return _Resp({"ok": True, "result": out})
        if method == "sendMessage":
            if self.fail_send:
                raise OSError("network down")
            return _Resp({"ok": True, "result": {"message_id": 42}})
        return _Resp({"ok": True, "result": True})

    def tap(self, nonce, verdict="y", user=555, chat=555, cb_id="cb1"):
        self.queue.append({"update_id": len(self.queue) + 1, "callback_query": {
            "id": cb_id, "data": f"ag:{verdict}:{nonce}",
            "from": {"id": user}, "message": {"message_id": 42, "chat": {"id": chat}},
        }})


class _Resp:
    def __init__(self, obj):
        self._obj = obj

    def read(self):
        return json.dumps(self._obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _config(tmp, **extra):
    d = os.path.join(tmp, "telegram.json")
    with open(d, "w", encoding="utf-8") as f:
        json.dump({"token": "123:ABC", "chat": "555", **extra}, f)
    os.chmod(d, 0o600)
    return d


class TimeoutPrecedenceTest(unittest.TestCase):
    """승인 대기시간 우선순위. systemd Environment= 로 지정한 값이 실제로 쓰여야 한다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("AUTORCODE_APPROVE_TIMEOUT")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("AUTORCODE_APPROVE_TIMEOUT", None)
        else:
            os.environ["AUTORCODE_APPROVE_TIMEOUT"] = self._prev

    def _load(self, **extra):
        return approve.load_config(_config(self.tmp, **extra))["timeout"]

    def test_file_value_is_used_when_env_absent(self):
        os.environ.pop("AUTORCODE_APPROVE_TIMEOUT", None)
        self.assertEqual(self._load(approveTimeout=90), 90)

    def test_env_overrides_file(self):
        # 배포 단위 override. 이게 안 먹으면 systemd Environment= 가 조용히 무시된다.
        os.environ["AUTORCODE_APPROVE_TIMEOUT"] = "45"
        self.assertEqual(self._load(approveTimeout=300), 45)

    def test_env_used_when_file_has_no_value(self):
        os.environ["AUTORCODE_APPROVE_TIMEOUT"] = "45"
        self.assertEqual(self._load(), 45)

    def test_default_when_neither_given(self):
        os.environ.pop("AUTORCODE_APPROVE_TIMEOUT", None)
        self.assertEqual(self._load(), approve.DEFAULT_TIMEOUT)

    def test_blank_env_falls_back_to_file(self):
        # 빈 문자열은 '지정 안 함' 이다. int("") 는 ValueError 이므로 방어해야 한다.
        os.environ["AUTORCODE_APPROVE_TIMEOUT"] = "   "
        self.assertEqual(self._load(approveTimeout=120), 120)


class GateConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_legacy_chat_becomes_single_entry_allowlist(self):
        # 예전 설정(chat 하나)은 목록 크기 1과 같다. 마이그레이션이 안전해야 한다.
        cfg = approve.load_config(_config(self.tmp))
        self.assertEqual(cfg["allowed"], [555])

    def test_rejects_group_readable_token_file(self):
        # 읽을 수 있는 사람이 내 명령 승인을 넘겨받을 수 있다. 닫혀야 한다.
        d = _config(self.tmp)
        os.chmod(d, 0o644)
        with self.assertRaises(approve.GateUnavailable) as cm:
            approve.load_config(d)
        self.assertIn("600", str(cm.exception))

    def test_rejects_empty_allowlist(self):
        d = _config(self.tmp, chat="", allowedChatIds=[])
        with self.assertRaises(approve.GateUnavailable):
            approve.load_config(d)

    def test_rejects_missing_file(self):
        with self.assertRaises(approve.GateUnavailable):
            approve.load_config(os.path.join(self.tmp, "nope.json"))

    def test_build_returns_none_when_unusable(self):
        # 예외 대신 None — 관점에서 '거부'와 동일하므로 조용히 넘어간다.
        self.assertIsNone(approve.build_from_config(os.path.join(self.tmp, "nope.json")))

    def _log_level(self, path):
        """build_from_config 가 남긴 로그의 최고 수준. None 이면 아무것도 안 남김."""
        with self.assertLogs("agent.approve", level="DEBUG") as cap:
            approve.build_from_config(path)
        levels = {r.levelno for r in cap.records}
        return max(levels) if levels else None

    def test_absent_config_is_info_not_warning(self):
        # 설정이 없다는 건 정상이다(텔레그램 안 쓰는 다수가 정상이다). WARNING 으로
        # 올리면 stderr 가 새어 'quiet 는 조용해야 한다' 를 깨고, 텔레그램 미설정
        # 사용자의 헤드리스 실행마다 경고가 쌓인다. 로컬에 telegram.json 이 있는 개발자
        # 에게는 이 문제가 invisible 이어서 CI 에서만 터졌다.
        self.assertLess(self._log_level(os.path.join(self.tmp, "nope.json")),
                        logging.WARNING)

    def test_broken_config_still_warns(self):
        # 반대로 '있는데 못 쓴다'는 설정 오류다. 조용히 삼키면 안 된다 — 사용자는
        # headless 인 줄 모르고 모든 승인이 조용히 거부되는 걸 모른다.
        p = os.path.join(self.tmp, "loose.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"token": "1:AA", "allowedChatIds": [1]}, f)
        os.chmod(p, 0o644)
        self.addCleanup(os.chmod, p, 0o600)
        self.assertGreaterEqual(self._log_level(p), logging.WARNING)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.gate = approve.ApprovalGate("t", [555], timeout=3, opener=self.api)
        # 폴러가 있어야 탭이 도착한다. 폴러 없이 호출하면 타임아웃으로 거부된다 —
        # 그 자체가 '게이트는 닫혀 있어야 한다'의 확인이기도 하다.
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def _nonce_from_send(self):
        for m, p in self.api.calls:
            if m == "sendMessage":
                return p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
        return None

    def test_approve_returns_true(self):
        result = {}

        def run():
            result["v"] = self.gate("rm -rf build 실행")

        t = threading.Thread(target=run)
        t.start()

        # sendMessage 가 나갈 때까지 기다린 뒤 탭한다.
        for _ in range(50):
            n = self._nonce_from_send()
            if n:
                break
            time.sleep(0.02)
        self.api.tap(n)
        t.join(5)
        self.assertTrue(result["v"])
        self.assertIn("실행합니다", self.api.answers)

    def test_deny_returns_false(self):
        result = {}
        t = threading.Thread(target=lambda: result.update(v=self.gate("rm 실행")))
        t.start()
        for _ in range(50):
            n = self._nonce_from_send()
            if n:
                break
            time.sleep(0.02)
        self.api.tap(n, verdict="n")
        t.join(5)
        self.assertFalse(result["v"])
        self.assertIn("거부했습니다", self.api.answers)

    def test_no_poller_means_deny(self):
        # 폴러를 띄우지 않으면 어떤 탭도 오지 않는다. 그럼에도 반드시 거부되어야 한다.
        lonely = approve.ApprovalGate("t", [555], timeout=1, opener=self.api)
        self.assertFalse(lonely("rm 실행"))

    def test_timeout_denies_and_removes_buttons(self):
        gate = approve.ApprovalGate("t", [555], timeout=1, opener=self.api)
        self.assertFalse(gate("rm 실행"))
        # 시간 초과 후 버튼이 회수돼야 나중에 눌려도 실행되지 않는다.
        edited = [p for m, p in self.api.calls if m == "editMessageText"]
        self.assertTrue(any(p.get("reply_markup", {}).get("inline_keyboard") == []
                            for p in edited))

    def test_send_failure_denies(self):
        # 전송 자체가 실패하면 승인이 된 적 없다. 예외로 새어나가지 않는다.
        self.api.fail_send = True
        self.assertFalse(self.gate("rm 실행"))

    def test_tap_from_other_user_is_refused(self):
        gate = approve.ApprovalGate("t", [555], timeout=1, opener=self.api)
        gate.start()
        self.addCleanup(gate.stop)
        result = {}
        t = threading.Thread(target=lambda: result.update(v=gate("rm 실행")))
        t.start()
        for _ in range(50):
            n = self._nonce_from_send()
            if n:
                break
            time.sleep(0.02)
        # 목록에 없는 사람이 누른다 — 그룹에 타인이 초대된 상황.
        # 이 탭이 결정을 내리면 안 된다: 끝까지 기다리다 타임아웃으로 거부되어야 한다.
        self.api.tap(n, user=999)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("v"), False)

    def test_replayed_nonce_is_rejected(self):
        result = {}
        t = threading.Thread(target=lambda: result.update(v=self.gate("rm 실행")))
        t.start()
        for _ in range(50):
            n = self._nonce_from_send()
            if n:
                break
            time.sleep(0.02)
        self.api.tap(n, cb_id="cb1")
        t.join(5)
        self.assertTrue(result["v"])
        # 같은 nonce 를 한 번 더 — 이미 회수됐으므로 거부돼야 한다.
        self.api.tap(n, cb_id="cb2")
        time.sleep(0.3)
        self.assertEqual(result["v"], True)

    def test_fabricated_nonce_is_rejected(self):
        gate = approve.ApprovalGate("t", [555], timeout=1, opener=self.api)
        gate.start()
        self.addCleanup(gate.stop)
        result = {}
        t = threading.Thread(target=lambda: result.update(v=gate("rm 실행")))
        t.start()
        for _ in range(50):
            n = self._nonce_from_send()
            if n:
                break
            time.sleep(0.02)
        # 아무도 요청하지 않은 nonce 로 탭 = 추측 공격. 거부되어야 하고,
        # 이 탭이 내 요청을 대신 승인해서는 안 된다.
        self.api.tap("deadbeef" * 4, cb_id="cbX")
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("v"), False)

    def test_poller_stops_on_409(self):
        # 같은 토큰으로 다른 폴러가 있으면 조용히 경쟁하지 않고 멈춘다.
        self.api.error_code = 409
        self.gate.start()
        time.sleep(0.4)
        self.gate.stop()

    def test_call_surfaces_error_code(self):
        self.api.error_code = 401
        with self.assertRaises(RuntimeError) as cm:
            self.gate._call("getMe", {})
        self.assertTrue(str(cm.exception).startswith("401:"))


if __name__ == "__main__":
    unittest.main()
