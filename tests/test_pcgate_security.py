"""pcgate 보안 회귀: 무토큰 fail-closed, 인증 비교, 로그의 토큰 마스킹."""
import unittest
import os
from unittest.mock import patch

from harness import pcgate


class PcGateSecurityTest(unittest.TestCase):
    def test_missing_configured_token_never_authenticates(self):
        with patch.object(pcgate, "TOKEN", ""):
            self.assertFalse(pcgate._token_matches(""))
            self.assertFalse(pcgate._token_matches("anything"))

    def test_token_match_and_mismatch(self):
        with patch.object(pcgate, "TOKEN", "secret-value"):
            self.assertTrue(pcgate._token_matches("secret-value"))
            self.assertFalse(pcgate._token_matches("wrong-value"))
            self.assertFalse(pcgate._token_matches(None))

    def test_dangerous_commands_require_human_telegram_approval(self):
        # confirm=true 는 채팅 모델이 스스로 세팅할 수 있어 사람의 확인이 아니다.
        # 위험 명령은 서버 쪽에서 텔레그램 승인을 강제한다 — 토큰을 아는 직접
        # 호출자도 게이트를 통과할 수 없어야 한다.
        class Yes:
            def __call__(self, q): return True

        class No:
            def __call__(self, q): return False

        with patch.object(pcgate, "_APPROVER", Yes()):
            self.assertIsNone(pcgate._dangerous_gate("shutdown", "pc", "", "203.0.113.9"))
        with patch.object(pcgate, "_APPROVER", No()):
            reason = pcgate._dangerous_gate("shutdown", "pc", "", "203.0.113.9")
            self.assertIn("사람 승인 없음", reason)

    def test_dangerous_gate_fails_closed_without_telegram_config(self):
        # 게이트를 못 만들면(텔레그램 설정 없음) 열리는 게 아니라 거부다.
        with patch.object(pcgate, "_APPROVER", None), \
             patch("harness.approve.build_from_config", return_value=None):
            reason = pcgate._dangerous_gate("shutdown", "pc", "", "203.0.113.9")
        self.assertIn("승인 게이트", reason)
        self.assertIn("거부", reason)

    def test_bearer_header_preferred_over_query_token(self):
        class Request:
            path = "/status?token=query-secret"
            headers = {"Authorization": "Bearer header-secret"}

        self.assertEqual(pcgate._request_token(Request()), "header-secret")

    def test_query_token_supported_for_existing_clients(self):
        class Request:
            path = "/result/id?token=query-secret"
            headers = {}

        self.assertEqual(pcgate._request_token(Request()), "query-secret")

    def test_result_cap_evicts_completed_but_not_pending_work(self):
        completed = {"old": {"status": "done", "ts": 1},
                     "new": {"status": "done", "ts": 2}}
        with patch.object(pcgate, "RESULTS", completed), patch.object(pcgate, "MAX_RESULTS", 2):
            with pcgate.LOCK:
                self.assertTrue(pcgate._make_result_room())
            self.assertNotIn("old", completed)
            self.assertIn("new", completed)

        pending = {"one": {"status": "pending", "ts": 1}}
        with patch.object(pcgate, "RESULTS", pending), patch.object(pcgate, "MAX_RESULTS", 1):
            with pcgate.LOCK:
                self.assertFalse(pcgate._make_result_room())
            self.assertIn("one", pending)

    def test_access_log_redacts_query_token_but_keeps_client_ip(self):
        handler = object.__new__(pcgate.Handler)
        handler.client_address = ("203.0.113.4", 12345)
        with self.assertLogs("autorcode.pcgate", level="INFO") as logs:
            handler.log_message('"%s" %s', "GET /status?token=supersecret HTTP/1.1", "200")
        output = "\n".join(logs.output)
        self.assertIn("203.0.113.4", output)
        self.assertIn("token=[redacted]", output)
        self.assertNotIn("supersecret", output)


if __name__ == "__main__":
    unittest.main()
