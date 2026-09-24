#!/usr/bin/env bash
# autorcode 설치 — "받아서 실행 한 번"으로 PATH에 등록한다.
#   bash install.sh            → ~/.local/bin/autorcode
#   bash install.sh /usr/bin   → 시스템(권한 필요 시 sudo)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${1:-$HOME/.local/bin}"

# 1) python3 존재 + 버전(3.9+) 확인
if ! command -v python3 >/dev/null 2>&1; then
  echo "[오류] python3 없음 — 설치 필요:"
  echo "  Ubuntu/Debian: sudo apt-get install -y python3"
  echo "  RHEL/CentOS:   sudo dnf install -y python3"
  exit 1
fi
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "[오류] python $PYVER 은 너무 낮음 — 3.9 이상 필요."
  echo "  예: sudo apt-get install -y python3.9  또는 pyenv/deadsnakes PPA 사용"
  exit 1
fi

# 2) 실행 권한 + 심링크
mkdir -p "$BIN"
chmod +x "$SRC/harness/cli.py" "$SRC/agent.py"
rm -f "$BIN/xx"
ln -sf "$SRC/harness/cli.py" "$BIN/autorcode"

# 3) 설치 검증 (심링크로 실제 실행)
if ! "$BIN/autorcode" help >/dev/null 2>&1; then
  echo "[오류] 설치 후 실행 검증 실패 — 다음으로 원인 확인:"
  echo "  $BIN/autorcode help"
  exit 1
fi
echo "설치 완료: $BIN/autorcode  (소스: $SRC)"
case ":$PATH:" in
  *":$BIN:"*) echo "바로 사용 가능 →  autorcode help / autorcode run phi4 '파일 목록 봐'" ;;
  *) echo "PATH 등록 후 사용:"
     for rc in ~/.bashrc ~/.zshrc; do [ -f "$rc" ] && { grep -q "$BIN" "$rc" || echo "export PATH=\"\$PATH:$BIN\"" >> "$rc"; echo "  $rc 에 추가함 (재로그인 또는 source)"; }; done ;;
esac

# 4) ollama 연결 상태 점검 (CLI 설치 자체는 성공이므로 경고만)
if ! "$BIN/autorcode" list >/dev/null 2>&1; then
  echo ""
  echo "[참고] ollama 연결 실패 — CLI는 정상 설치됨. 모델 실행만 아래로 준비:"
  echo "  curl -fsSL https://ollama.com/install.sh | sh   # ollama 설치"
  echo "  systemctl --user start ollama 2>/dev/null || sudo systemctl start ollama"
fi
