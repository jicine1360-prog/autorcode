"""반복 실패 감지·같은 호스트 복사 진단 테스트."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import loops  # noqa: E402

# 실제 사고 명령 — 카운터만 selfN/rsN 으로 바뀌며 231~240 스텝 반복됐다.
INCIDENT = (
    'timeout 15 ssh -o BatchMode=yes sehane@100.113.203.75 '
    '\'setsid bash -c "rsync -e \\"ssh -o BatchMode=yes\\" '
    'sehane@100.113.203.75:/tmp/gen.json /tmp/self137.json > /tmp/rs147.log 2>&1" '
    '</dev/null >/dev/null 2>&1 & sleep 8; tail -1 /tmp/rs147.log; '
    'stat -c %s /tmp/self137.json 2>/dev/null\' 2>&1 | head -3'
)


class TestSignature(unittest.TestCase):
    def test_counter_changes_are_same_attempt(self):
        a = INCIDENT.replace("137", "138").replace("147", "148")
        self.assertEqual(loops.signature("bash", {"command": INCIDENT}),
                         loops.signature("bash", {"command": a}))

    def test_different_commands_differ(self):
        self.assertNotEqual(loops.signature("bash", {"command": "ls /tmp"}),
                            loops.signature("bash", {"command": "df -h"}))

    def test_non_bash(self):
        self.assertEqual(loops.signature("read_file", {"path": "a1.txt"}),
                         loops.signature("read_file", {"path": "a2.txt"}))


class TestFailed(unittest.TestCase):
    def test_exit_zero_with_rsync_error_is_failure(self):
        """setsid 는 exit=0 을 돌려주지만 출력의 rsync error 는 실패다."""
        out = ("[exit=0]\nrsync error: unexplained error (code 255) at io.c(232) "
               "[Receiver=3.2.7]")
        self.assertTrue(loops.failed(out))

    def test_exit_zero_plain_output_is_success(self):
        self.assertFalse(loops.failed("[exit=0]\n(출력 없음)"))
        self.assertFalse(loops.failed("[exit=0]\nhello world"))

    def test_nonzero_exit(self):
        self.assertTrue(loops.failed("[exit=255]\nssh: connect to host x port 22"))

    def test_gate_error(self):
        self.assertTrue(loops.failed("[오류] [거부] 사용자 미승인"))


class TestSelfCopyHint(unittest.TestCase):
    def test_incident_suggests_cp(self):
        hint = loops.self_copy_hint(INCIDENT)
        self.assertIn("cp /tmp/gen.json /tmp/self137.json", hint)
        self.assertIn("같은 호스트", hint)

    def test_plain_ssh_command_has_no_hint(self):
        self.assertEqual(loops.self_copy_hint("ssh sehane@100.113.203.75 'ls -la /tmp'"), "")

    def test_remote_to_remote_scp_has_no_hint(self):
        cmd = "ssh -o BatchMode=yes h 'scp h:/tmp/a h:/tmp/b'"
        self.assertEqual(loops.self_copy_hint(cmd), "")

    def test_localhost_without_ssh(self):
        hint = loops.self_copy_hint("rsync localhost:/tmp/a /tmp/b")
        self.assertIn("cp /tmp/a /tmp/b", hint)

    def test_non_copy_command(self):
        self.assertEqual(loops.self_copy_hint("grep rsync README.md"), "")


class TestBackgroundHint(unittest.TestCase):
    def test_setsid(self):
        self.assertIn("exit=0", loops.background_hint(INCIDENT))

    def test_nohup(self):
        self.assertTrue(loops.background_hint("nohup rsync a b >/tmp/x.log 2>&1 &"))

    def test_plain_command(self):
        self.assertEqual(loops.background_hint("sleep 1; ls -la"), "")


class TestLoopGuard(unittest.TestCase):
    def test_steer_then_abort(self):
        g = loops.LoopGuard(limit=3)
        self.assertEqual(g.observe("sig", True), "")
        self.assertEqual(g.observe("sig", True), "steer")
        self.assertEqual(g.observe("sig", True), "abort")

    def test_success_resets_streak(self):
        g = loops.LoopGuard(limit=3)
        self.assertEqual(g.observe("sig", True), "")
        self.assertEqual(g.observe("sig", True), "steer")
        self.assertEqual(g.observe("sig", False), "")
        self.assertEqual(g.observe("sig", True), "")
        self.assertEqual(g.observe("sig", True), "steer")

    def test_different_signature_resets(self):
        g = loops.LoopGuard(limit=3)
        self.assertEqual(g.observe("a", True), "")
        self.assertEqual(g.observe("a", True), "steer")
        self.assertEqual(g.observe("b", True), "")   # 새 시도는 1회부터

    def test_disabled(self):
        g = loops.LoopGuard(limit=0)
        for _ in range(10):
            self.assertEqual(g.observe("sig", True), "")


if __name__ == "__main__":
    unittest.main()
