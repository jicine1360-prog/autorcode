"""브리지의 작업 루트 격리.

배경: 브리지는 예전에 `req.get("root")` 를 검증 없이 샌드박스 루트로 썼다. 그래서
--workspace 를 아무리 좁혀도 클라이언트가 root 를 대신 지정하면 샌드박스를 그대로
벗어났다. 실제로 --workspace /tmp/narrow-ws 로 띄운 브리지에 {"root": "/home/"} 를
실어 보내 ~/.bashrc 를 읽었다. 토큰 하나면 ~/.ssh/id_ed25519 나
~/.autorcode/telegram.json 같은 샌드박스 밖 비밀까지 닿는다.

여기서는 그 필드가 구멍이 되지 않는지, 그리고 정당한 사용(workspace 안의 하위
디렉터리 지정)은 계속되는지를 고정한다. 보안 통제라 escape 방향만 확인하면
다음에 누군가 "편하니까 그냥 root 받아쓰자" 하고 되돌릴 수 있다.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import bridge  # noqa: E402


class ResolveRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = os.path.join(self.tmp, "ws")
        self.sub = os.path.join(self.ws, "sub")
        os.makedirs(self.sub)
        # bridge 는 기동 시 _WORKSPACE 를 채운다. 여기서 그대로 흉내낸다.
        self._prev = bridge._WORKSPACE
        bridge._WORKSPACE = os.path.realpath(self.ws)
        self.addCleanup(self._restore)

    def _restore(self):
        bridge._WORKSPACE = self._prev

    def test_empty_request_uses_server_workspace(self):
        # 요청이 root 를 안 주면 서버가 정한 곳을 쓴다.
        self.assertEqual(bridge._resolve_root(""), bridge._WORKSPACE)
        self.assertEqual(bridge._resolve_root(None), bridge._WORKSPACE)

    def test_subdirectory_inside_workspace_is_allowed(self):
        # Open WebUI 가 하위 디렉터리를 넘기는 정상 사용 케이스. 막으면 안 된다.
        self.assertEqual(bridge._resolve_root(self.sub), os.path.realpath(self.sub))

    def test_parent_escape_is_refused(self):
        with self.assertRaises(ValueError):
            bridge._resolve_root(self.tmp)

    def test_sibling_prefix_is_refused(self):
        # 'ws-evil' 이 'ws' 로 시작하지만 'ws/' 로는 시작하지 않는다. 접두사 비교를
        #startswith(root) 로 하면 여기서 샌드박스를 벗어난다.
        sibling = self.ws + "-evil"
        os.makedirs(sibling, exist_ok=True)
        with self.assertRaises(ValueError):
            bridge._resolve_root(sibling)

    def test_home_is_refused_when_workspace_is_narrow(self):
        with self.assertRaises(ValueError):
            bridge._resolve_root(os.path.expanduser("~"))

    def test_root_is_refused(self):
        with self.assertRaises(ValueError):
            bridge._resolve_root("/")

    def test_symlink_out_of_workspace_is_refused(self):
        # workspace 안의 심볼릭 링크가 밖을 가리켜도 realpath 로 무효화돼야 한다.
        link = os.path.join(self.ws, "out")
        os.symlink(self.tmp, link)
        with self.assertRaises(ValueError):
            bridge._resolve_root(link)

    def test_env_var_reference_is_refused(self):
        # confine 는 $HOME 같은 환경변수 참조를 경로에 금지한다. 그대로 통과시키지 않는다.
        with self.assertRaises(ValueError):
            bridge._resolve_root("$HOME")


if __name__ == "__main__":
    unittest.main()
