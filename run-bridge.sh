#!/usr/bin/env bash
# autorcode 브리지 상시 가동 래퍼 — systemd 등록 전까지 백그라운드로 실행
set -e
cd /home/hoony/agent-harness
TOKEN="${AUTORCODE_BRIDGE_TOKEN:-$(cat /home/hoony/.autorcode/bridge.token 2>/dev/null)}"
if [ -z "$TOKEN" ]; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(20))")
  echo "$TOKEN" > /home/hoony/.autorcode/bridge.token
  chmod 600 /home/hoony/.autorcode/bridge.token
  echo "새 토큰 생성: $TOKEN"
fi
exec env AUTORCODE_BRIDGE_TOKEN="$TOKEN" \
  AGENT_WORKSPACE=/home/hoony \
  /home/hoony/.hermes/hermes-agent/venv/bin/python3 \
  -m harness.bridge --host 127.0.0.1 --port 8787 --workspace /home/hoony