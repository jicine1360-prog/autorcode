# autorcode — 모델 라우팅 + 도구 실행 하네스 (ollama 스타일)

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

## 동작 구조
`판단(LLM) ↔ JSON 프로토콜 ↔ 하네스(도구 실행/권한/샌드박스)`

- **라우팅**: 요청 키워드 점수로 fast/smart, `run <model>`은 단일 모델 고정
- **프로토콜**: A)단일도구 B)병렬도구(actions) C)done — 3단 파서+셀프리페어
- **안전**: bash 화이트리스트 + 저술 명령 승인게이트(`AGENT_PERMS=yolo|balanced|strict`),
  명령 차단패턴, 경로 샌드박스(cwd 기준), RLIMIT+프로세스그룹 킬
- **컨텍스트**: 토큰 추정 예산 트리밍, `max_tokens`로 CoT 폭주 차단

## 설정 (환경변수)
```
AGENT_BASE_URL/AGENT_API_KEY   openai 전환: https://api.openai.com/v1 + sk-...
AGENT_MODEL_FAST/SMART         ollama 모델명 (기본 phi4:latest / qwen3.8:27b-hunmin-64k)
AGENT_PERMS                    yolo | balanced(기본) | strict
AGENT_MAX_STEPS(15) AGENT_BASH_TIMEOUT(30) AGENT_CONTEXT_TOKENS(20000)
AGENT_RLIMIT_MEM_MB(4096) AGENT_RLIMIT_NPROC(128) AGENT_MAX_TOKENS(800)
```
전체 목록: `autorcode help`

## 개발/테스트
```bash
python3 -m compileall -q harness agent.py
python3 -m unittest discover -s tests -v     # CI: GitHub Actions (.github/workflows/test.yml)
```

## License
MIT
