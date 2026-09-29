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

    def test_destructive_actions_require_server_side_opt_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PCGATE_DANGEROUS_ENABLED", None)
            self.assertFalse(pcgate._dangerous_enabled())
        with patch.dict(os.environ, {"PCGATE_DANGEROUS_ENABLED": "true"}):
            self.assertTrue(pcgate._dangerous_enabled())
        with patch.dict(os.environ, {"PCGATE_DANGEROUS_ENABLED": "1"}):
            self.assertTrue(pcgate._dangerous_enabled())

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
