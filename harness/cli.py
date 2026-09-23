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

from harness import agent_core, config, llm, tools  # noqa: E402
from harness.progress import Progress, short  # noqa: E402


def _host() -> str:
    h = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not h.startswith("http"):
        h = "http://" + h
    return h.rstrip("/")


def _input(prompt: str) -> str:
    """터미널 로케일과 무관하게 raw 바이트로 읽어 디코딩한다.

    input()은 sys.stdin의 로케일 인코딩(보통 utf-8)으로 읽다가 한글 euc-kr 계열
    바이트에서 UnicodeDecodeError로 죽는 경우가 있어 raw 버퍼로 우회한다.
    utf-8 → cp949 순으로 시도하고, 그래도 실패하면 replace 폴백 (예외 없음).
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    raw = sys.stdin.buffer.readline()
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()


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
    if arg in names:
        return arg
    if arg + ":latest" in names:
        return arg + ":latest"
    matches = [n for n in names if n.startswith(arg + ":")]
    if len(matches) == 1:
        return matches[0]
    return arg if ":" in arg else None


def _resolve_backend(model: str):
    """프로바이더 결정: AGENT_BASE_URL > 모델에 '/' 포함 → OpenRouter > 로컬 ollama."""
    if os.getenv("AGENT_BASE_URL"):
        return os.getenv("AGENT_BASE_URL"), os.getenv("AGENT_API_KEY") or "ollama"
    if "/" in model:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit(
                "[오류] '/' 포함 모델은 OpenRouter 경유입니다. "
                "OPENROUTER_API_KEY=sk-or-... 를 설정하거나 "
                "(AGENT_BASE_URL/AGENT_API_KEY로 임의 OpenAI 호환 서버도 가능)")
        return "https://openrouter.ai/api/v1", key
    return _host() + "/v1", "ollama"


# ---------------- subcommands ----------------

HELP_TEXT = """autorcode — 모델 라우팅 + 도구 실행 에이전트 (로컬 ollama + 클라우드 공존)

기본 사용
  autorcode list                    설치된 모델 (크기/로드상태)
  autorcode run                     로드된(또는 첫) 모델로 REPL
  autorcode run phi4 "지시"         모델 지정 단발 실행
  autorcode run phi4                그 모델로 REPL
  autorcode run phi4 --yes          승인 자동 (화이트리스트는 유지)
  autorcode run phi4 --quiet        과정 출력 끄기 (기본: 스텝마다 실시간 표시)
  autorcode run phi4 --details      도구 결과 미리보기 확대 (3줄 → 12줄)
  autorcode run phi4 --no-stream    SSE 미지원 서버에 일반 JSON 요청
  autorcode run phi4 --session h.jsonl   히스토리 저장/재개
  autorcode chat phi4               도구 없는 단순 채팅
  autorcode doctor                  서버/메모리/GPU 자가진단

GPU 없이 즉시 사용 (OpenRouter — 모델명 '/' 포함)
  OPENROUTER_API_KEY=sk-or-... autorcode run deepseek/deepseek-chat-v3 "분석해"
  ollama가 꺼져 있어도 OPENROUTER_API_KEY만 있으면 자동 전환됨
  기본 클라우드 모델은 OPENROUTER_MODEL(기본 deepseek/deepseek-chat-v3)로 변경

실행 과정
  모델 요청 즉시 대기 표시 → 응답 수신량(글자 수) → 도구 실행 → 결과/시간 표시
  터미널에서는 경과 시간 갱신, 파이프/로그에서는 5초마다 진행 상태 한 줄
  진행은 stderr, 최종 답변은 stdout. 원시 추론이나 미완성 JSON은 표시하지 않음
  대화 중 /status /tools /steps on|off /details on|off /help 사용 가능
  업데이트 뒤에는 exit 후 다시 실행해야 새 기능이 적용됨

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
  MAX_TOKENS(2048) RLIMIT_MEM_MB(4096) SESSION, PERMS
  SHOW_STEPS(1) SHOW_DETAILS(0) STREAM(1) — 0/1로 표시·스트리밍 설정
  OPENROUTER_API_KEY=sk-or-...  OPENROUTER_MODEL(기본 deepseek/deepseek-chat-v3)

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
    key = os.getenv("OPENROUTER_API_KEY")
    if key:
        print(f"openrouter: 키 설정됨 (…{key[-4:]}) — '/' 모델(클라우드) 즉시 사용 가능")
    return 0


def _make_cfg(model: str, rest) -> config.Config:
    cfg = config.load()
    cfg.base_url, cfg.api_key = _resolve_backend(model)
    cfg.model_fast = model
    cfg.model_smart = model
    if rest.yes:
        cfg.auto_yes = True
    if getattr(rest, "quiet", False):
        cfg.show_steps = False
    if getattr(rest, "details", False):
        cfg.show_details = True
    if getattr(rest, "no_stream", False):
        cfg.stream = False
    if rest.session:
        cfg.session_file = rest.session
    return cfg


def cmd_run(args):
    display = config.load()
    progress = Progress(display.show_steps and not args.quiet,
                        display.show_details or args.details)
    with progress.activity("[연결] 로컬 모델 목록 확인"):
        names = _names()
    model, prompt = args.model, list(args.prompt or [])
    cloud_key = os.getenv("OPENROUTER_API_KEY")
    if not names:
        if cloud_key:
            if "/" in model:
                pass  # 명시적 클라우드 모델 → 그대로 진행
            elif not model:
                model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3")
                print(f"[연결] ollama 응답 없음 → OpenRouter 사용 ({model})", file=sys.stderr)
            else:
                print(f"[오류] ollama가 응답하지 않아 로컬 모델 {model!r}을 쓸 수 없습니다. "
                      f"클라우드로 실행하려면 'deepseek/deepseek-chat-v3' 같은 '/' 모델을 지정하세요.",
                      file=sys.stderr)
                return 1
    elif model and "/" not in model:
        resolved = _match_model(model, names)
        if resolved is None:  # 모델 아닌 단어 → 프롬프트로 강등
            prompt.insert(0, model)
            model = ""
        else:
            model = resolved
    if not model:
        loaded = _loaded()
        pool = loaded | set(names)
        model = sorted(pool, key=lambda n: (n not in loaded, n))[0] if pool \
            else config.load().model_fast
    cfg = _make_cfg(model, args)
    agent = agent_core.Agent(cfg, confirmer=_ask, progress=progress)
    head = f"autorcode run {model}  (권한 {cfg.permissions_mode}{'/auto-yes' if cfg.auto_yes else ''})"

    try:
        if prompt:
            progress.event(head)
            print(agent.run(" ".join(prompt)), flush=True)
            return 0

        print(f"=== {head} === 도구 {len(tools.TOOLS)}개 · /help 도움말 · exit 종료", flush=True)
        while True:
            try:
                task = _input("당신> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not task:
                continue
            if task.lower() in ("exit", "quit", "종료"):
                return 0
            if repl_command(task, agent):
                continue
            print(f"\n{agent.run(task)}\n", flush=True)
    finally:
        agent.close()


def repl_command(task, agent):
    """로컬 명령은 모델 호출 없이 즉시 처리한다."""
    parts = task.split()
    command = parts[0] if parts else ""
    if command not in ("/help", "/status", "/tools", "/steps", "/details"):
        return False
    if command == "/help":
        print("/status 상태 · /tools 도구 목록 · /steps on|off 과정 표시 · "
              "/details on|off 결과 확대 · exit 종료")
    elif command == "/tools":
        print(tools.schema_text())
    elif command == "/status":
        cfg = agent.cfg
        print(f"모델: {cfg.model_fast} / {cfg.model_smart}\n작업 위치: {cfg.workspace_root}\n"
              f"도구 {len(tools.TOOLS)}개 · 권한 {cfg.permissions_mode} · "
              f"과정 {'on' if agent.progress.enabled else 'off'} · "
              f"상세 {'on' if agent.progress.details else 'off'} · "
              f"SSE {'on' if cfg.stream else 'off'}")
    elif len(parts) == 2 and parts[1] in ("on", "off"):
        value = parts[1] == "on"
        if command == "/steps":
            agent.cfg.show_steps = agent.progress.enabled = value
        else:
            agent.cfg.show_details = agent.progress.details = value
        print(f"{command}: {parts[1]}")
    else:
        print(f"사용법: {command} on|off")
    return True


def _ask(why: str) -> bool:
    try:
        print(f"\n[승인?] {short(why, 400)}\n  y/N: ", end="", file=sys.stderr, flush=True)
        return input().strip().lower() in ("y", "yes", "ㄱ")
    except EOFError:
        return False


def cmd_chat(args):
    names = _names()
    model = _match_model(args.model, names) or args.model
    messages = [{"role": "system", "content": "간결하게 한국어로 답한다."}]
    base_url, api_key = _resolve_backend(model)
    client = llm.OpenAICompatibleLLM(base_url, api_key, 120, 2, 0.3)
    print(f"=== autorcode chat {model} (도구 없음/{'OpenRouter' if '/' in model else 'ollama'}) === exit 종료")
    while True:
        try:
            q = _input("당신> ").strip()
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
    run_parser = p
    p.add_argument("model", nargs="?", default="", help="ollama 모델명 (생략 시 로드/목록 우선)")
    p.add_argument("prompt", nargs="*", help="한 번 실행할 지시")
    p.add_argument("--yes", action="store_true", help="승인 자동")
    p.add_argument("--quiet", action="store_true", help="과정 출력 끔 (결과만)")
    p.add_argument("--details", action="store_true", help="도구 결과 미리보기 확대")
    p.add_argument("--no-stream", action="store_true", help="SSE 대신 일반 JSON 응답 사용")
    p.add_argument("--verbose", action="store_true", help="진단 로그 출력")
    p.add_argument("--session", help="히스토리 jsonl")
    p.set_defaults(fn=cmd_run, cmd="run")

    p = sub.add_parser("list", help="로컬 모델 목록")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("chat", help="도구 없는 단순 채팅")
    p.add_argument("model")
    p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("doctor", help="환경 진단")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("help", help="전체 치트시트")
    p.set_defaults(fn=cmd_help)

    # run의 모델/프롬프트 사이에도 --details 등의 옵션을 둘 수 있다.
    args = (run_parser.parse_intermixed_args(sys.argv[2:])
            if sys.argv[1:2] == ["run"] else ap.parse_args())
    if args.cmd in ("run", "chat"):
        config.setup_logging(getattr(args, "verbose", False))
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
