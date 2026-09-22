#!/usr/bin/env bash
# autorcode 시연 녹화 시나리오 — asciinema pty 안에서 실행됨
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
export AGENT_SHOW_DETAILS=0
export AGENT_MAX_STEPS=8

DEMO=/tmp/opencode/demo-repo
rm -rf "$DEMO"
mkdir -p "$DEMO"
cd "$DEMO"
printf 'hello world\n' > sample.txt
printf 'no,1\nja,2\n' > data.csv

clear
printf '\n$ autorcode run phi4 "파일들을 확인하고 demo_memo.txt에 메모를 남겨줘"\n'
autorcode run phi4 "현재 디렉토리에 어떤 파일이 있는지 확인한 뒤, demo_memo.txt 파일을 만들어 'autorcode 데모 완료'라고 저장해줘" --yes < /dev/null
printf '\n\n$ cat demo_memo.txt\n'
cat demo_memo.txt
printf '\n'
echo "──────────────────────────────────────────────────"
echo "  녹화 완료. (진행표시 → 도구 실행 → 결과 저장)"