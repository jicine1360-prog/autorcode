"""브리지(/tool) 경로의 권한 판정 — 웹에서 셸이 즉시 돌던 구멍의 회귀 테스트.

배경: 브리지의 /tool 은 tools.execute() 를 바로 호출했다. agent_core.Agent 가 쓰는
permissions.check_bash/check_write 판정을 아예 거치지 않았기 때문에, 토큰 하나만
알면 웹에서 `rm`, `curl`, `systemctl` 같은 상태 변경 명령이 승인 없이 실행됐다.
CLI 경로(/run)에는 권한 판정이 있었으니 같은 서버에서 경로만 바꿔도 뚫렸다.

여기서는 브리지가 deny 로 막는 것과, 읽기 전용은 통과하는 것, 그리고 게이트가
없을 때는 '확인' 필요 작업이 전부 막히는 것(fail-closed)을 고정한다. escape
방향만 확인하면 나중에 "WebUI 에서 매번 승인 누르기 귀찮다" 하고 되돌릴 수 있다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import bridge  # noqa: E402


class BridgePermissionGateTest(unittest.TestCase):
    def setUp(self):
        self._prev_mode = bridge._PERMS_MODE
        self._prev_conf = bridge._CONFIRMER
        bridge._PERMS_MODE = "balanced"
        bridge._CONFIRMER = None
        self.addCleanup(self._restore)

    def _restore(self):
        bridge._PERMS_MODE = self._prev_mode
        bridge._CONFIRMER = self._prev_conf

    # --- escape 방향: 막혀야 한다 ---

    def test_state_changing_bash_denied_without_gate(self):
        denied = bridge._gate("bash", {"command": "rm -rf work/tmp"})
        self.assertIsNotNone(denied)
        self.assertIn("승인 게이트", denied)

    def test_confirm_bin_denied_without_gate(self):
        for cmd in ("systemctl restart nginx", "curl http://x/y", "pip install x",
                    "mv a b", "chmod 777 /etc/passwd"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bridge._gate("bash", {"command": cmd}), cmd)

    def test_policy_deny_ignored_even_with_approving_gate(self):
        # 승인을 하는 게이트가 붙어도 permissions 의 deny 는 무효다.
        # 게이트는 '사용자 승인' 뿐 '정책 위반'을 승인할 수는 없다.
        bridge._CONFIRMER = lambda _q: True
        for cmd in ("shutdown now", "dd if=/dev/zero of=/dev/sda"):
            with self.subTest(cmd=cmd):
                denied = bridge._gate("bash", {"command": cmd})
                self.assertIsNotNone(denied, cmd)
                self.assertTrue(denied.startswith("[거부]"))

    def test_rm_rf_root_relies_on_safety_layer(self):
        # `rm -rf /` 는 permissions 에서 confirm 이고(사용자 승인 필요), 실제로는
        # tools.execute 안의 safety.check_bash 가 막는다. 두 층이 분리된 것이라
        # 브리지 게이트는 그 둘을 대체하지 않는다. 여기서는 그 경계를 고정한다.
        from harness import safety
        bridge._CONFIRMER = lambda _q: True
        self.assertIsNone(bridge._gate("bash", {"command": "rm -rf /"}))
        with self.assertRaises(safety.UnsafeCommand):
            safety.check_bash("rm -rf /")

    def test_credential_access_denied_even_with_gate(self):
        bridge._CONFIRMER = lambda _q: True
        denied = bridge._gate("bash", {"command": "cat ~/.ssh/id_ed25519"})
        self.assertIsNotNone(denied)

    def test_strict_mode_write_denied_without_gate(self):
        bridge._PERMS_MODE = "strict"
        self.assertIsNotNone(bridge._gate("write_file", {"path": "a.txt", "content": "x"}))
        self.assertIsNotNone(bridge._gate("edit_file", {"path": "a.txt"}))

    # --- 정상 동작: 통과해야 한다 ---

    def test_readonly_bash_passes_without_confirmation(self):
        for cmd in ("ls -la", "df -h", "ps aux", "cat README.md", "grep -r x ."):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bridge._gate("bash", {"command": cmd}), cmd)

    def test_readonly_tools_never_gated(self):
        # 게이트는 bash/쓰기만 본다. 파일 열람은 언제나 통과해야 한다.
        for name, args in (("read_file", {"path": "a"}), ("list_dir", {}),
                           ("grep_files", {"pattern": "x"}), ("web_search", {"query": "q"})):
            with self.subTest(name=name):
                self.assertIsNone(bridge._gate(name, args))

    def test_write_allowed_in_balanced(self):
        # balanced 는 파일쓰기를 허용하므로 게이트를 거치지 않는다.
        self.assertIsNone(bridge._gate("write_file", {"path": "a.txt", "content": "x"}))

    def test_approving_gate_lets_confirm_bash_through(self):
        bridge._CONFIRMER = lambda _q: True
        self.assertIsNone(bridge._gate("bash", {"command": "systemctl restart nginx"}))

    def test_refusing_gate_blocks_confirm_bash(self):
        bridge._CONFIRMER = lambda _q: False
        denied = bridge._gate("bash", {"command": "systemctl restart nginx"})
        self.assertIsNotNone(denied)
        self.assertIn("미승인", denied)

    def test_yolo_mode_allows_bash(self):
        bridge._PERMS_MODE = "yolo"
        self.assertIsNone(bridge._gate("bash", {"command": "systemctl restart nginx"}))


if __name__ == "__main__":
    unittest.main()
