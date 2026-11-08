"""프로젝트 지침 자동 로드 — Claude Code 의 CLAUDE.md 처럼 작업 공간의 지침
파일을 시스템 프롬프트에 실어 보낸다.

- 전역: ~/.autorcode/AGENTS.md (모든 작업 공간에 공통, 먼저·약하게 실린다)
- 작업 공간: 현재 폴더에서 루트("/")까지 올라가며 AGENTS.md, CLAUDE.md,
  .autorcode/instructions.md 를 주운다. 작업 공간에 가까운(구체적인) 지침이
  뒤에 강하게 실린다.
- 총 상한 AGENT_INSTRUCTIONS_MAX_CHARS (기본 8000자).
- 지침은 사용자 제공 파일이다 — 안전 규칙·승인 정책을 덮지 않는다는 안내와
  함께 프롬프트에 들어간다(agent_core 가 붙인다).
"""
import os

CANDIDATES = ("AGENTS.md", "CLAUDE.md",
              os.path.join(".autorcode", "instructions.md"))
GLOBAL_DIR = os.path.join("~", ".autorcode")


def find(workspace_root: str) -> list:
    """적용할 지침 파일 경로 목록. 전역 → 먼 조상 → 작업 공간(최근) 순."""
    files = []
    seen = set()

    def add(path: str) -> None:
        real = os.path.realpath(path)
        if real not in seen and os.path.isfile(path):
            seen.add(real)
            files.append(path)

    for name in CANDIDATES:
        add(os.path.expanduser(os.path.join(GLOBAL_DIR, name)))

    chain = []
    current = os.path.realpath(workspace_root or os.getcwd())
    while True:
        chain.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(chain):  # / 쪽(광범위) 먼저, 작업 공간 마지막
        for name in CANDIDATES:
            add(os.path.join(directory, name))
    return files


def load(workspace_root: str, limit: int = 0) -> tuple:
    """(합친 지침 텍스트, 로드된 파일 목록). 파일 없으면 ("", [])."""
    if limit <= 0:
        limit = int(os.getenv("AGENT_INSTRUCTIONS_MAX_CHARS", "8000"))
    parts, loaded, used = [], [], 0
    for path in find(workspace_root):
        budget = limit - used
        if budget <= 64:
            break
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(budget).strip()
        except OSError:
            continue
        if not text:
            continue
        header = f"### {path}\n"
        text = text[:budget - len(header)]
        parts.append(header + text)
        loaded.append(path)
        used += len(header) + len(text) + 2
    return "\n\n".join(parts), loaded
