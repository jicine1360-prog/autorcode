"""Read-only server management tools stay scoped and do not invoke a shell."""
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness import tools


class ServerManagementToolsTest(unittest.TestCase):
    def test_find_files_stays_within_workspace_and_matches_names(self):
        with tempfile.TemporaryDirectory() as root:
            import os
            os.mkdir(os.path.join(root, "nested"))
            with open(os.path.join(root, "nested", "report.txt"), "w") as f:
                f.write("ok")
            out = tools.execute("find_files", {"path": ".", "pattern": "*.txt"}, root, 1000, 5)
            self.assertIn("nested/report.txt", out.replace("\\", "/"))
            escaped = tools.execute("find_files", {"path": "../", "pattern": "*"}, root, 1000, 5)
            self.assertIn("샌드박스 밖", escaped)

    def test_disk_usage_is_read_only_and_reports_volume(self):
        with tempfile.TemporaryDirectory() as root:
            out = tools.execute("disk_usage", {"path": "."}, root, 1000, 5)
            self.assertIn("[디스크]", out)
            self.assertIn("여유", out)

    def test_service_status_rejects_unrecognized_filter(self):
        out = tools.execute("service_status", {"state": "running; shutdown"}, "/tmp", 1000, 5)
        self.assertIn("[오류]", out)
        self.assertIn("state", out)

    def test_service_status_uses_fixed_argv_without_shell(self):
        fake = subprocess.CompletedProcess([], 0, "qq-tts.service active running\n", "")
        with patch.object(tools.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(tools.subprocess, "run", return_value=fake) as run:
            out = tools.execute("service_status", {"state": "active"}, "/tmp", 1000, 5)
        self.assertIn("qq-tts.service", out)
        self.assertEqual(run.call_args.args[0][0:2], ["/usr/bin/systemctl", "--user"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_service_logs_redacts_credential_patterns(self):
        fake = subprocess.CompletedProcess([], 0,
            "bad token 1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 and sk-or-v1-abcdefghijklmnopqrstuvwxyz123456\n", "")
        with patch.object(tools.shutil, "which", return_value="/usr/bin/journalctl"), \
             patch.object(tools.subprocess, "run", return_value=fake):
            out = tools.execute("service_logs", {"service": "test.service"}, "/tmp", 1000, 5)
        self.assertIn("[telegram-token-redacted]", out)
        self.assertIn("[api-key-redacted]", out)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", out)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", out)

    def test_process_limit_is_bounded(self):
        output = "header\n" + "".join(f"{i} 0 proc 0 0\n" for i in range(50))
        fake = subprocess.CompletedProcess([], 0, output, "")
        with patch.object(tools.shutil, "which", return_value="/usr/bin/ps"), \
             patch.object(tools.subprocess, "run", return_value=fake):
            out = tools.execute("process_list", {"limit": 10000}, "/tmp", 1000, 5)
        self.assertEqual(len(out.splitlines()), 31)


if __name__ == "__main__":
    unittest.main()
