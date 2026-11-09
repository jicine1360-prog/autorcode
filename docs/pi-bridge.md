# pi 브리지 (autorcode → pi 위임)

autorcode 가 pi 코딩 에이전트를 JSONL RPC 하위프로세스로 빌려 쓴다. (`harness/pigate.py`)

## 도구 2개 추가 (기존 동작 변경 없음)
- `pi_task` : `{task, cwd?, write?, session?, model?, timeout?}` — pi에게 작업 위임
  - 기본 **읽기 전용** (`pi --tools read,grep,find,ls`) — 파일/셸 사용 불가, 안전
  - `write:true` → pi가 write/edit/bash 사용 가능. `agent_core._gate` 가 파일쓰기 등급으로 승인 요구 (strict 모드·텔레그램 승인게이트 동일)
  - `session:true` → pi 세션 파일 저장(재시작 후 이어쓰기), 기본은 `--no-session`
  - `cwd` 기본값 = autorcode 작업공간
- `pi_status` : `{reset?}` — 브리지 프로세스 상태/종료

## 특징
- 프로세스 상주 재사용(2번째 호출부터 기동 오버헤드 없음), 인자 조합이 바뀌면 자동 재기동
- 이벤트는 `agent_settled` 까지 기다린다(재시도·컴팩션 후속 포함), 시간초과 시 프로세스 재생
- pi의 자동 컴팩션·128k 컨텍스트를 그대로 씀 → 여러 파일 요약/리팩터 후보 찾기를 한 번에
- 모델은 기본값(pi 설정 = strata 상주 qwen3.8) 그대로, `model` 로 지정 가능

## 실측 (2026-11-09)
- 읽기 전용: tools.py 를 pi가 직접 read/grep 해서 도구 24개 목록 정확히 답변 — 31초(기동 포함)
- `write:true`: /tmp 에 hello.txt 생성 확인 — 10초
