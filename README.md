# autorcode — 모델 라우팅 + 도구 실행 하네스 (ollama 스타일)

[![test](https://github.com/jicine1360-prog/autorcode/actions/workflows/test.yml/badge.svg)](https://github.com/jicine1360-prog/autorcode/actions/workflows/test.yml)

> ⚠️ **학습용/프로토타입 하네스입니다 — 보안 경계가 아닙니다.**
> - bash는 화이트리스트·차단패턴·RLIMIT으로 방어하지만, 새로운 우회 수단이 나오면 뚫린다.
>   자격증명(.ssh/.aws 등) 접근은 필터링하지만 **이건 억지력이지 보장이 아니다.**
> - 프로덕션은 컨테이너/namespace 격리(docker·firejail·seccomp)와 감사 로그 위에 얹으세요.
> - 동명의 다른 제품(autocode 등)과 무관한 개인 프로젝트입니다.

openai 과금 없이 로컬 ollama로 도는 에이전트. 설치 한 번으로 `autorcode` 명령.

## 설치
```bash
cd agent-harness && bash install.sh     # → ~/.local/bin/autorcode
```
요구사항: python3 (stdlib만), ollama 서버.

## 사용
```bash
autorcode help                   # 전체 치트시트
autorcode doctor                 # 환경 진단 (서버/모델/메모리)
autorcode list                   # 로컬 모델 목록 (크기/로드상태)
autorcode run phi4 "파일 목록 봐" # 단발 실행 — 부분명(phi4) 자동 매칭
autorcode run phi4               # 대화형 REPL
autorcode chat phi4              # 도구 없는 단순 채팅
autorcode run                    # 인자 없음 → 로드된 모델 우선
```

## 실행 과정을 보면서 사용하기

기본값으로 모델 요청부터 최종 답변까지 진행 상태를 보여줍니다.
아래는 **화면 형식 예시**입니다(시간과 내용은 실행마다 달라집니다).

```text
[시작] qwen3-next:80b-128k · fast · 작업 위치: /home/hoony
[1/15] 모델 응답 대기 · qwen3-next:80b-128k
  | 모델 응답 수신 중 · 183자 · 6.2s
[1] 모델 응답 수신 완료 · 7.1s · 230자
[1.1 web_search · AI 에이전트] 실행 중
  [완료] 1.1 web_search · AI 에이전트 · 1.4s · 820자
    │ 1. 검색 결과 제목
    │ https://example.com/article
[2/15] 모델 응답 대기 · qwen3-next:80b-128k
...
[완료] 3스텝 · 도구 요청 2회
```

- 터미널에서는 한 줄에 상태와 경과 시간이 계속 갱신됩니다. 파이프/로그로 받으면
  ANSI 제어문자 없이 5초마다 진행 상태가 한 줄씩 기록됩니다.
- SSE를 지원하는 모델 서버에서는 **응답이 도착하는 동안** 수신 글자 수를 갱신합니다.
  수신량은 토큰 수나 완료율이 아닙니다. 모델의 원시 추론·미완성 JSON은 표시하지 않습니다.
- 도구별 실행 대상, 시간, 결과 미리보기, 실패/거부/시간초과를 구분합니다.
  독립 조회의 병렬 결과는 완료되는 즉시 보입니다. 쓰기·셸·영상이 포함된 묶음은
  순서대로 실행하고, 승인 질문은 동시에 띄우지 않습니다.
- 과정은 **stderr**, 최종 답변은 **stdout**입니다.

```bash
autorcode run qwen3-next:80b-128k --details  # 결과 미리보기 3줄 → 12줄
autorcode run phi4 "파일 목록 봐" --quiet  # 과정 없이 최종 답변만
autorcode run phi4 --no-stream              # SSE 미지원 서버용
autorcode run phi4 "파일 목록 봐" 2>progress.log
```

대화 중에는 모델 호출 없이 다음 명령을 사용할 수 있습니다.

| 명령 | 기능 |
|---|---|
| `/status` | 모델, 작업 위치, 도구 수, 표시 옵션 확인 |
| `/tools` | 현재 프로세스에 등록된 도구와 인자 목록 |
| `/steps on` / `/steps off` | 과정 표시 켜기/끄기 |
| `/details on` / `/details off` | 결과 미리보기 확대/축소 |
| `/help` | 대화 명령 도움말 |

**업데이트 전에 켜둔 REPL은 `exit` 후 다시 실행하세요.** 실행 중인 프로세스에
새 코드와 도구 목록이 자동 반영되지는 않습니다. 대기 표시는 모델 로딩과 추론 중 어느
단계인지 추측하지 않으며, 서버가 실제 응답을 보내기 전에는 '모델 응답 대기'로 표시합니다.

## 동작 구조
`판단(LLM) ↔ JSON 프로토콜 ↔ 하네스(도구 실행/권한/샌드박스)`

- **라우팅**: 요청 키워드 점수로 fast/smart, `run <model>`은 단일 모델 고정
- **프로토콜**: A)단일도구 B)병렬도구(actions) C)done — 3단 파서+셀프리페어
- **도구**: bash / 파일(read·write·edit·list·grep) + **web_search**(DDG, API키 불필요) /
  **web_fetch**(웹페이지) / **youtube**(yt-dlp 메타+자막 — 영상 "보기")
- **안전**: bash 화이트리스트 + 저술 명령 승인게이트(`AGENT_PERMS=yolo|balanced|strict`),
  명령 차단패턴, 경로 샌드박스(cwd 기준), RLIMIT+프로세스그룹 킬,
  웹 도구 SSRF 가드(사설 IP/내부 서비스 접근 거부)
- **컨텍스트**: 토큰 추정 예산 트리밍, `max_tokens`로 CoT 폭주 차단

## 설정 (환경변수)
```
AGENT_BASE_URL/AGENT_API_KEY   openai 전환: https://api.openai.com/v1 + sk-...
AGENT_MODEL_FAST/SMART         ollama 모델명 (기본 phi4:latest / qwen3.8:27b-hunmin-64k)
AGENT_PERMS                    yolo | balanced(기본) | strict
AGENT_MAX_STEPS(15) AGENT_BASH_TIMEOUT(30) AGENT_CONTEXT_TOKENS(20000)
AGENT_RLIMIT_MEM_MB(4096) AGENT_RLIMIT_NPROC(128) AGENT_MAX_TOKENS(800)
AGENT_SHOW_STEPS(1) AGENT_SHOW_DETAILS(0) AGENT_STREAM(1)   # 0/1
```
전체 목록: `autorcode help`

## 개발/테스트
```bash
python3 -m compileall -q harness agent.py
python3 -m unittest discover -s tests -v     # CI: GitHub Actions (.github/workflows/test.yml)
```

## License
MIT
