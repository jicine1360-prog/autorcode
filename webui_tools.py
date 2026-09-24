"""
autorcode × Open WebUI — 도구 브리지
=====================================
Open WebUI 채팅에서 autorcode(호스트 서버의 LLM 에이전트) 도구를 직접 부른다.

사용법:
  1. ~/agent-harness 에서 브리지 기동:
       AUTORCODE_BRIDGE_TOKEN=<토큰> python3 -m harness.bridge --workspace /home/hoony
  2. Open WebUI → 작업공간 → 도구(Tools) → 이 파일을 "도구로 추가(코드로 등록)".
  3. 채팅에서 모델에 이 도구들을 활성화하고,
     - list_dir / read_file → 서버 파일 탐색/읽기
     - web_search / web_fetch → 웹 자료 조사
     - excel_summary → .xlsx 업로드 파일 요약(경로 명시)
     - visit_report → 지정 폴더 PDF들을 읽고 엑셀 요약본 생성
     등을 자연어로 요청.

보안:
  - 호스트 브리지는 127.0.0.1 + Bearer 토큰으로만 응답.
  - 이 Tools는 Open WebUI 내부에서 브리지로 프록시만 하므로
    autorcode의 샌드박스(RLIMIT/화이트리스트/타임아웃)가 그대로 적용된다.
  - bash/write_file 등 파괴적 도구: 브리지 기본정책이 호출허용이나, 서비스 구성에선
    AUTORCODE_BRIDGE_ALLOW=false 시 읽기/조사 도구만 열리도록 제어 가능.
"""
from typing import Callable

import httpx
import os

# ---- 브리지 주소/토큰 (Open WebUI 환경변수 권장) ----
BRIDGE_URL = os.getenv("AUTORCODE_BRIDGE_URL", "http://127.0.0.1:8787")
BRIDGE_TOKEN = os.getenv("AUTORCODE_BRIDGE_TOKEN", "")
WORKSPACE = os.getenv("AGENT_WORKSPACE", "/home/hoony")


def _call(tool: str, args: dict, root: str = "", timeout: int = 120) -> str:
    if not BRIDGE_TOKEN:
        return "[설정오류] AUTORCODE_BRIDGE_TOKEN 미설정 — 관리자에게 문의"
    try:
        r = httpx.post(
            f"{BRIDGE_URL}/tool",
            json={"tool": tool, "args": args, "root": root or WORKSPACE,
                  "timeout": timeout},
            headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
            timeout=timeout + 15,
        )
        data = r.json()
    except Exception as e:
        return f"[브리지 연결 실패] {type(e).__name__}: {e}"
    if not data.get("ok"):
        return f"[오류] {data.get('error', '알 수 없는 오류')}"
    return data.get("result", "")


# ---------------- 기본 파일/웹 도구 ----------------

def list_dir(filesystem: str, path: str = ".") -> str:
    """
    서버의 폴더(디렉터리)에 있는 파일/하위폴더 목록을 반환한다.
    - filesystem: 항상 "local" 고정 (브리지 작업공간의 한 곳)
    - path: 상대경로(예: "/data") 또는 프로젝트 폴더명
    """
    return _call("list_dir", {"path": path})


def read_file(filesystem: str, path: str, max_lines: int = 200) -> str:
    """
    서버의 텍스트 파일(코드/로그/문서)을 줄번호와 함께 읽는다.
    - filesystem: "local"
    - path: 절대경로 또는 작업공간 내 상대경로
    """
    return _call("read_file", {"path": path, "max_lines": max_lines})


def web_search(query: str, max_results: int = 5) -> str:
    """키워드로 웹 검색(위키/뉴스/문서) 결과 제목+URL 목록을 반환한다."""
    return _call("web_search", {"query": query, "max_results": max_results})


def web_fetch(url: str) -> str:
    """웹페이지 URL을 읽어 본문 텍스트(뉴스 기사·문서)로 반환한다."""
    return _call("web_fetch", {"url": url})


# ---------------- 엑셀 통합 ----------------

def excel_summary(filesystem: str, path: str, sheet: str = "") -> str:
    """
    .xlsx 엑셀 파일을 요약한다. 시트명/행수/열제목/주요값/숫자 컬럼 합계를 반환.
    - filesystem: "local"
    - path: .xlsx 파일 절대경로
    - sheet: 특정 시트를 지정(비우면 첫 시트)
    """
    return _call("excel_summary", {"path": path, "sheet": sheet}, timeout=180)


def excel_write(filesystem: str, path: str, content: str) -> str:
    """
    엑셀(.xlsx) 요약·정리 파일을 생성한다. 내용은 Markdown 표 형식으로 작성하면
    서버가 표를 시트로 변환한다. 결과 저장 경로를 반환한다.
    - filesystem: "local"
    - path: 저장할 파일 경로(예: "/data/report.xlsx")
    """
    return _call("excel_write", {"path": path, "content": content}, timeout=180)


# ---------------- 관리/지원 봇 ----------------

def support_status() -> str:
    """서비스 상태·정책을 반환한다. (사용자 문의 자동응답용)"""
    return """
서비스 상태: 정상 운영 중
운영 방식: 완전 자동화(관리자 개입 예약)
문의 경로: 이메일 jicine1360@gmail.com (24시간 이내 답변)
전화 지원: 없음 (지원봇/이메일만 제공)
정책: 민감 데이터는 서버 밖으로 전송되지 않음 (로컬 우선)
"""


# 판매·기능 소개는 아래 tool로 분리해 모델이 상황에 맞게 호출
def service_intro() -> str:
    """서비스 소개·기능·요금 정책 요약을 반환한다."""
    return """
autorcode — 내 서버가 일하는 LLM 에이전트
- 핵심: 웹검색/파일/엑셀/브라우저 자동화를 자연어로 위임, 결과를 파일로 생성
- 로컬 우선: 모델·데이터가 서버 밖으로 나가지 않음
- 문의: jicine1360@gmail.com (자동 응대 후 필요한 경우 관리자가 답변)
"""