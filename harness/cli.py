#!/usr/bin/env python3
"""autorcode — ollama 스타일 로컬 에이전트 CLI.

  autorcode run [model] [prompt]  # 모델 지정 실행/REPL (ollama run 처럼)
  autorcode list                  # 설치된 ollama 모델 목록
  autorcode chat model            # 도구 없는 단순 대화
  autorcode doctor                # 환경 자가진단
  autorcode help                  # 전체 치트시트
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 설치 위치(심볼릭링크)에 상관없이 harness 패키지를 찾는다
_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from harness import agent_core, config, llm  # noqa: E402


def _host() -> str:
    h = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not h.startswith("http"):
        h = "http://" + h
    return h.rstrip("/")


def _get(path: str, timeout: float = 5):
    req = urllib.request.Request(_host() + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _names() -> list:
    try:
        return [m["name"] for m in _get("/api/tags").get("models", [])]
    except Exception:
        return []


def _loaded() -> set:
    try:
        return {m["name"] for m in _get("/api/ps").get("models", [])}
    except Exception:
        return set()


def _match_model(arg: str, names: list):
    """arg가 모델 이름인지 판별 — 'phi4'면 'phi4:latest' 매칭, ':' 포함이면 통과."""
    if any(arg == n or arg + ":latest" == n or arg in n for n in names):
        return next(n for n in names if n == arg or n.startswith(arg + ":"))
    return arg if ":" in arg else None


# ---------------- subcommands ----------------

HELP_TEXT = """autorcode — 모델 라우팅 + 도구 실행 에이전트 (openai 과금 없이 로컬 ollama)

기본 사용
  autorcode list                    설치된 모델 (크기/로드상태)
  autorcode run                     로드된(또는 첫) 모델로 REPL
  autorcode run phi4 "지시"         모델 지정 단발 실행
  autorcode run phi4                그 모델로 REPL
  autorcode run phi4 --yes          승인 자동 (화이트리스트는 유지)
  autorcode run phi4 --session h.jsonl   히스토리 저장/재개
  autorcode chat phi4               도구 없는 단순 채팅
  autorcode doctor                  서버/메모리/GPU 자가진단

도구 프로토콜 (모델의 1스텝 출력 규식)
  A {"tool":"bash","args":{"command":"ls"}}        단독
  B {"actions":[{...},{...}]}                      병렬 (최대 4개)
  C {"done":true,"answer":"..."}                    종료

도구: bash / read_file / write_file / edit_file / list_dir / grep_files
      web_search(웹검색·키불필요) / web_fetch(웹페이지) / youtube(메타+자막)
격리: cwd 샌드박스 + 차단패턴(sudo rm -rf /, curl|sh, 포크폭탄…) +
      RLIMIT(CPU/MEM/FSIZE/NPROC) + 타임아웃 시 프로세스그룹째 KILL +
      웹 도구 SSRF 가드(사설 IP·내부 포트 접근 거부)

권한 모드 (AGENT_PERMS)
  balanced(기본)  읽기 화이트리스트 통과, 저술적(rm·python3·git·curl…)은 y/N 승인
  yolo            블랙리스트만 적용
  strict          bash·파일쓰기 전부 승인 필요

환경변수 (AGENT_*)
  MODEL_FAST(기본 phi4:latest) MODEL_SMART(qwen3.8:27b-hunmin-64k)
  API_TIMEOUT(120) MAX_STEPS(15) BASH_TIMEOUT(30) CONTEXT_TOKENS(20000)
  MAX_TOKENS(800) RLIMIT_MEM_MB(4096) SESSION, PERMS

유료 API 전환
  AGENT_BASE_URL=https://api.openai.com/v1 AGENT_API_KEY=sk-... \\
    autorcode run gpt-4o "분석해"
"""


def cmd_help(_args):
    print(HELP_TEXT)
    return 0


def cmd_list(_args):
    try:
        models = _get("/api/tags").get("models", [])
    except Exception as e:
        print(f"ollama 서버에 연결할 수 없습니다 ({_host()}): {e}")
        print("  시작: ollama serve")
        return 1
    loaded = _loaded()
    print(f"{'NAME':<28} {'SIZE':>7}  {'STATUS'}")
    for m in sorted(models, key=lambda x: x["name"]):
        gb = m.get("size", 0) / 2**30
        print(f"{m['name']:<28} {gb:>6.1f}G  {'●loaded' if m['name'] in loaded else ''}")
    return 0


def cmd_doctor(_args):
    print(f"python   : {sys.version.split()[0]}  {sys.executable}")
    print(f"cli 위치 : {os.path.dirname(_HERE)}")
    try:
        models = _get("/api/tags").get("models", [])
        loaded = _loaded()
        print(f"ollama   : {_host()} OK — 모델 {len(models)}개, 로드중 {sorted(loaded)}")
    except Exception as e:
        print(f"ollama   : {_host()} 연결실패 ({e}) — `ollama serve` 실행 필요")
    try:
        mi = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mi[k] = int(v.split()[0])
        print(f"메모리   : total {mi['MemTotal']//2**20}GB / avail {mi['MemAvailable']//2**20}GB")
    except Exception:
        pass
    print(f"경고     : " + ("AGENT_BASE_URL/OLLAMA_HOST 미설정 → autorcode run은 ollama 기본으로 동작" if not (os.getenv("AGENT_BASE_URL") or os.getenv("OLLAMA_HOST")) else "AGENT_BASE_URL 설정됨 — autorcode run이 그 엔드포인트 우선 사용"))
    return 0


def _make_cfg(model: str, rest) -> config.Config:
    cfg = config.load()
    cfg.base_url = os.getenv("AGENT_BASE_URL") or (_host() + "/v1")
    cfg.api_key = os.getenv("AGENT_API_KEY") or "ollama"
    cfg.model_fast = model
    cfg.model_smart = model
    if rest.yes:
        cfg.auto_yes = True
    if rest.session:
        cfg.session_file = rest.session
    return cfg


def cmd_run(args):
    names = _names()
    model, prompt = args.model, list(args.prompt or [])
    if model:
        resolved = _match_model(model, names)
        if resolved is None:  # 모델 아닌 단어 → 프롬프트로 강등
            prompt.insert(0, model)
            model = ""
        else:
            model = resolved
    if not model:
        pool = _loaded() | set(names)
        model = sorted(pool, key=lambda n: (n not in _loaded(), n))[0] if pool \
            else config.load().model_fast
    cfg = _make_cfg(model, args)
    agent = agent_core.Agent(cfg, confirmer=_ask)
    head = f"autorcode run {model}  (권한 {cfg.permissions_mode}{'/auto-yes' if cfg.auto_yes else ''})"

    if prompt:
        print(head)
        print(agent.run(" ".join(prompt)))
        return 0

    print(f"=== {head} === exit 입력 시 종료")
    while True:
        try:
            task = input("당신> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if task.lower() in ("exit", "quit", "종료", ""):
            return 0
        try:
            print(f"\n{agent.run(task)}\n")
        except llm.LLMError as e:
            print(f"[오류] {e} — 모델 크기를 줄이거나 ollama ps로 로드 현황 확인\n")


def _ask(why: str) -> bool:
    try:
        return input(f"\n[승인?] {why}\n  y/N: ").strip().lower() in ("y", "yes", "ㄱ")
    except EOFError:
        return False


def cmd_chat(args):
    names = _names()
    model = _match_model(args.model, names) or args.model
    messages = [{"role": "system", "content": "간결하게 한국어로 답한다."}]
    client = llm.OpenAICompatibleLLM(_host() + "/v1", "ollama", 120, 2, 0.3)
    print(f"=== autorcode chat {model} (도구 없음/ollama급 응답) === exit 종료")
    while True:
        try:
            q = input("당신> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if q.lower() in ("exit", "quit", ""):
            return 0
        messages.append({"role": "user", "content": q})
        try:
            a = client.chat(messages, model)
        except llm.LLMError as e:
            print(f"[오류] {e}")
            continue
        messages.append({"role": "assistant", "content": a})
        print(f"보조개> {a}\n")


def main() -> int:
    ap = argparse.ArgumentParser(prog="autorcode", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="모델 실행: autorcode run [model] [prompt]")
    p.add_argument("model", nargs="?", default="", help="ollama 모델명 (생략 시 로드/목록 우선)")
    p.add_argument("prompt", nargs="*", help="한 번 실행할 지시")
    p.add_argument("--yes", action="store_true", help="승인 자동")
    p.add_argument("--session", help="히스토리 jsonl")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("list", help="로컬 모델 목록")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("chat", help="도구 없는 단순 채팅")
    p.add_argument("model")
    p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("doctor", help="환경 진단")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("help", help="전체 치트시트")
    p.set_defaults(fn=cmd_help)

    args = ap.parse_args()
    cfg = config.load()
    config.setup_logging(getattr(args, "verbose", False))
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
