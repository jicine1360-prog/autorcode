# 세션 컴팩션 (harness/compact.py)

컨텍스트 예산의 `compact_trigger`(기본 0.7)를 넘으면 옛 턴을 LLM 요약 1장으로 압축.
- 최근 턴은 `compact_keep`(기본 0.3)만큼 원문 보존, 경계는 항상 user 메시지부터 → tool 페어링 안 잘림
- 요약 포맷: [진행 중]/[완료]/[결정]/[파일]/[주의]/[다음 단계] (이전 요약과 이어쓰기)
- 요약 실패 시 naive_summary 로 축소가 계속 동작, 빈 응답 모델은 max_tokens 올려 1회 재시도
- 압축 후 세션 JSONL 재작성(마커 {"compaction":n} + 보존 턴), 재로딩 시 마커 무시
- 환경변수: AGENT_COMPACT=0|1, AGENT_COMPACT_TRIGGER, AGENT_COMPACT_KEEP
- 실측: 1,577tok → 495tok (원문 4턴 보존, 사실 정확 보존)
