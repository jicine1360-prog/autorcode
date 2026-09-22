#!/usr/bin/env bash
# autorcode 설치 — "받아서 실행 한 번"으로 PATH에 등록한다.
#   bash install.sh            → ~/.local/bin/autorcode
#   bash install.sh /usr/bin   → 시스템(권한 필요 시 sudo)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${1:-$HOME/.local/bin}"

command -v python3 >/dev/null || { echo "python3 필요"; exit 1; }
mkdir -p "$BIN"
chmod +x "$SRC/harness/cli.py" "$SRC/agent.py"
rm -f "$BIN/xx"
ln -sf "$SRC/harness/cli.py" "$BIN/autorcode"

echo "설치 완료: $BIN/autorcode  (소스: $SRC)"
case ":$PATH:" in
  *":$BIN:"*) echo "바로 사용 가능 →  autorcode help / autorcode run phi4 '파일 목록 봐'" ;;
  *) echo "PATH 등록 후 사용:"
     for rc in ~/.bashrc ~/.zshrc; do [ -f "$rc" ] && { grep -q "$BIN" "$rc" || echo "export PATH=\"\$PATH:$BIN\"" >> "$rc"; echo "  $rc 에 추가함 (재로그인 또는 source)"; }; done ;;
esac
