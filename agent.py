#!/usr/bin/env python3
"""모델 라우팅 + 하네스 에이전트.

  python3 agent.py                              # REPL (모의 모드)
  python3 agent.py --provider ollama "ls 해봐"  # 로컬 ollama 무료 사용
  python3 agent.py --provider ollama --yes "..."# 자동승인(화이트리스트는 유지)
  AGENT_MODEL_FAST=qwen3-coder:30b ...          # 모델 커스텀

openai 전환: AGENT_BASE_URL=https://api.openai.com/v1 AGENT_API_KEY=sk-...
권한 모드: AGENT_PERMS=yolo|balanced(기본)|strict
"""
import argparse
import logging
import os
import sys

from harness import config


def main() -> int:
    ap = argparse.ArgumentParser(description="Model-routing agent harness")
    ap.add_argument("task", nargs="?", help="단발 실행할 요청")
    ap.add_argument("--provider", choices=["ollama", "openai"],
                    help="llm 프리셋 (ollama=http://127.0.0.1:11434/v1)")
    ap.add_argument("--yes", action="store_true", help="확인 승인 자동화")
    ap.add_argument("--session", help="히스토리 jsonl 저장/재개")
    ap.add_argument("--tools", action="store_true", help="도구 목록")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="진행 출력 끄기")
    ap.add_argument("--details", action="store_true", help="도구 결과 미리보기 확대")
    ap.add_argument("--no-stream", action="store_true", help="SSE 대신 일반 JSON 응답 사용")
    args = ap.parse_args()

    if args.provider == "ollama":
        os.environ["AGENT_PROVIDER"] = "ollama"
    cfg = config.load()
    cfg.auto_yes = args.yes
    if args.quiet:
        cfg.show_steps = False
    if args.details:
        cfg.show_details = True
    if args.no_stream:
        cfg.stream = False
    if args.session:
        cfg.session_file = args.session
    config.setup_logging(args.verbose)

    from harness import tools as tools_mod
    if args.tools:
        print(tools_mod.schema_text())
        return 0

    from harness.agent_core import Agent

    def ask(why: str) -> bool:
        ans = input(f"\n[승인?] {why}\n  허용하시겠어요? [y/N] ").strip().lower()
        return ans in ("y", "yes", "ㄱ")

    agent = Agent(cfg, confirmer=ask if sys.stdin.isatty() else None)

    if args.task:
        print(agent.run(args.task))
        return 0

    mode = ("MOCK (규칙 기반 더미)" if cfg.use_mock else
            f"LLM  fast={cfg.model_fast} / smart={cfg.model_smart} @ {cfg.base_url}")
    print(f"=== 라우팅 에이전트 하네스 === 샌드박스: {cfg.workspace_root}")
    print(f"    모드: {mode} | 권한: {cfg.permissions_mode}"
          + (" +자동승인" if cfg.auto_yes else ""))
    print("    exit 입력 시 종료\n")
    while True:
        try:
            task = input("당신> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            continue
        if task.lower() in ("exit", "quit", "종료"):
            break
        try:
            print(f"\n{agent.run(task)}\n")
        except Exception:
            logging.getLogger("agent").exception("작업 실행 오류")
            print("[오류] 로그 확인\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
