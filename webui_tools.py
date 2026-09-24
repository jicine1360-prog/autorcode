"""
autorcode × Open WebUI — 도구 브리지 (Open WebUI Tools 형식)
=============================================================
Open WebUI(작업공간 → 도구 → 새 도구)에 이 파일을 붙여넣고 저장한 뒤,
채팅에서 모델에 도구를 활성화하면 autorcode(호스트 브리지)를 자연어로 부를 수 있다.

준비:
  1. 호스트에서 브리지 기동 (systemd: autorcode-bridge.service, 127.0.0.1:8787)
  2. Open WebUI → 작업공간 → 도구 → 이 코드 붙여넣기 → 저장
  3. 도구 설정(Valves)에서 BRIDGE_TOKEN을 호스트의 ~/.autorcode/bridge.token 값으로 설정
     (또는 컨테이너 env AUTORCODE_BRIDGE_TOKEN 사용)
  4. 새 채팅 → 사용 모델에 이 도구들 활성화

도구 목록:
  - list_dir / read_file    서버 파일 탐색·읽기
  - web_search / web_fetch  웹 검색·페이지 읽기
  - excel_summary / excel_write  .xlsx 요약·생성
  - support_status / service_intro  문의 자동응답용 안내

보안:
  - 호스트 브리지는 127.0.0.1 + Bearer 토큰로만 응답.
  - 이 Tools는 Open WebUI 내부에서 브리지로 프록시만 하므로
    autorcode의 샌드박스(RLIMIT/화이트리스트/타임아웃/SSRF 가드)가 그대로 적용된다.
  - 파괴적 도구(bash/write_file 등)는 브리지 ALLOW_LIST에 없으면 차단된다.
"""
import asyncio
import json
import os
import time
import urllib.request
from typing import Callable

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        BRIDGE_URL: str = Field(
            default="http://127.0.0.1:8787",
            description="autorcode 브리지 주소 (localhost)",
        )
        BRIDGE_TOKEN: str = Field(
            default="",
            description="Bearer 토큰 — 호스트의 ~/.autorcode/bridge.token 값"
                        " (환경변수 AUTORCODE_BRIDGE_TOKEN도 대체 사용)",
        )
        WORKSPACE: str = Field(
            default="/home/hoony",
            description="브리지 작업공간 루트 (파일 도구의 상한)",
        )
        PCGATE_URL: str = Field(
            default="http://127.0.0.1:8791",
            description="PC 게이트 주소 (Windows PC 에이전트 명령 큐)",
        )
        PCGATE_TOKEN: str = Field(
            default="bde4ee9d179781029f638641b99f921e1e316b3e3b09e7e7",
            description="PC 게이트 토큰 — 호스트 ~/.autorcode/pcgate.token",
        )

    def __init__(self):
        self.valves: Tools.Valves = self.Valves()
        self._token = os.getenv("AUTORCODE_BRIDGE_TOKEN", "")

    # ---------------- 내부 헬퍼 ----------------

    async def _call(self, tool: str, args: dict, timeout: int = 120) -> str:
        token = self._token or self.valves.BRIDGE_TOKEN or os.getenv(
            "AUTORCODE_BRIDGE_TOKEN", ""
        )
        if not token:
            return "[설정오류] BRIDGE_TOKEN 미설정 — 도구 설정(Valves)에서 호스트 토큰을 입력하세요."
        body = json.dumps({
            "tool": tool,
            "args": args,
            "root": self.valves.WORKSPACE,
            "timeout": timeout,
        }).encode()
        req = urllib.request.Request(
            f"{self.valves.BRIDGE_URL}/tool",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            return await asyncio.to_thread(self._post, req, timeout + 15)
        except Exception as e:
            return f"[브리지 연결 실패] {type(e).__name__}: {e}"

    @staticmethod
    def _post(req: urllib.request.Request, timeout: int) -> str:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            return f"[오류] {data.get('error', '알 수 없는 오류')}"
        return data.get("result", "")

    # ---------------- 기본 파일/웹 도구 ----------------

    async def list_dir(self, path: str = ".") -> str:
        """
        서버 폴더(작업공간 내)의 파일/하위폴더 목록을 반환한다.
        - path: 작업공간 기준 상대경로 (예: "/" 또는 "data")
        """
        return await self._call("list_dir", {"path": path}, timeout=60)

    async def read_file(self, path: str, max_lines: int = 200) -> str:
        """
        서버의 텍스트 파일(코드/로그/문서)을 줄번호와 함께 읽는다.
        - path: 작업공간 내 절대/상대경로
        - max_lines: 읽을 최대 줄 수
        """
        return await self._call(
            "read_file", {"path": path, "max_lines": max_lines}, timeout=60
        )

    async def web_search(self, query: str, max_results: int = 5) -> str:
        """키워드로 웹 검색(위키/뉴스/문서). 제목+URL 목록을 반환한다."""
        return await self._call(
            "web_search", {"query": query, "max_results": max_results}, timeout=60
        )

    async def web_fetch(self, url: str) -> str:
        """웹페이지 URL을 읽어 본문 텍스트(기사/문서)로 반환한다."""
        return await self._call("web_fetch", {"url": url}, timeout=90)

    async def youtube(self, url: str, get: str = "info") -> str:
        """
        유튜브 영상 분석.
        - url: youtube.com 또는 youtu.be 링크
        - get: "info"=메타(제목/채널/길이/조회수/설명), "transcript"=자막 전체
        자막으로 영상 내용 요약·분석이 가능하다.
        """
        return await self._call("youtube", {"url": url, "get": get}, timeout=120)

    # ---------------- 엑셀 통합 ----------------

    async def excel_summary(self, path: str, sheet: str = "") -> str:
        """
        .xlsx 엑셀 파일 요약: 시트명/행수/열제목/주요값/숫자 합계.
        - path: 작업공간 내 .xlsx 파일 경로
        - sheet: 특정 시트 지정(비우면 첫 시트)
        """
        return await self._call(
            "excel_summary", {"path": path, "sheet": sheet}, timeout=180
        )

    async def excel_write(self, path: str, content: str) -> str:
        """
        엑셀(.xlsx) 파일 생성. content는 Markdown 표 형식으로 작성하면
        서버가 표를 시트로 변환한다. 저장 경로를 반환.
        """
        return await self._call(
            "excel_write", {"path": path, "content": content}, timeout=180
        )

    # ---------------- 관리/지원 봇 ----------------

    async def support_status(self) -> str:
        """서비스 상태·정책 안내 (문의 자동응답용)."""
        return (
            "서비스 상태: 정상 운영 중\n"
            "운영 방식: 완전 자동화(관리자 개입 예약)\n"
            "문의 경로: 이메일 jicine1360@gmail.com (24시간 이내 답변)\n"
            "전화 지원: 없음 (지원봇/이메일만 제공)\n"
            "정책: 민감 데이터는 서버 밖으로 전송되지 않음(로컬 우선)"
        )

    async def service_intro(self) -> str:
        """서비스 소개·기능 요약 (도입 안내용)."""
        return (
            "autorcode — 내 서버가 일하는 LLM 에이전트\n"
            "- 핵심: 웹검색/파일/엑셀 자동화를 자연어로 위임, 결과를 파일로 생성\n"
            "- 로컬 우선: 모델·데이터가 서버 밖으로 나가지 않음\n"
            "- 문의: jicine1360@gmail.com (자동 응대 후 필요한 경우 관리자가 답변)"
        )

    # ---------------- 내 PC 관리 (Windows 에이전트) ----------------

    async def _pc(self, cmd: str, text: str = "", confirm: bool = False,
                  timeout: int = 45) -> str:
        body = json.dumps({
            "pc": "pc", "cmd": cmd, "text": text,
            "confirm": confirm, "token": self.valves.PCGATE_TOKEN,
        }).encode()
        req = urllib.request.Request(
            f"{self.valves.PCGATE_URL}/submit", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            resp = await asyncio.to_thread(self._pc_post, req, 10)
            if not resp.get("ok"):
                return f"[오류] {resp.get('error', '명령 제출 실패')}"
            cmd_id = resp["cmd_id"]
            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(2)
                r = await asyncio.to_thread(self._pc_result, cmd_id, 5)
                if r.get("status") == "done":
                    return r.get("output") or "(빈 응답)"
            return (f"[무응답] {cmd} 명령이 {timeout}초 내 결과 없음 — "
                    "PC가 꺼졌거나 에이전트가 멈췄을 수 있다 (재시작/종료 명령은 정상일 수 있음)")
        except Exception as e:
            return f"[PC 게이트 연결 실패] {type(e).__name__}: {e} (에이전트 실행 중인지 확인: schtasks /run /tn AutorPC)"

    @staticmethod
    def _pc_post(req: urllib.request.Request, timeout: int) -> dict:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def _pc_result(cmd_id: str, timeout: int) -> dict:
        token = Tools._PC_TOKEN_REF[0]
        req = urllib.request.Request(
            f"{Tools._PC_URL_REF[0]}/result/{cmd_id}?token={token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"status": "pending", "err": str(e)}

    _PC_TOKEN_REF = [""]
    _PC_URL_REF = ["http://127.0.0.1:8791"]

    async def pc_status(self) -> str:
        """내 PC(Windows) 상태 조회: OS/CPU 사용률/메모리/가동시간."""
        Tools._PC_TOKEN_REF[0] = self.valves.PCGATE_TOKEN
        Tools._PC_URL_REF[0] = self.valves.PCGATE_URL
        return await self._pc("sysinfo")

    async def pc_disk(self) -> str:
        """내 PC 디스크 사용량 (드라이브별 사용/전체 GB, 퍼센트)."""
        Tools._PC_TOKEN_REF[0] = self.valves.PCGATE_TOKEN
        Tools._PC_URL_REF[0] = self.valves.PCGATE_URL
        return await self._pc("disk")

    async def pc_procs(self) -> str:
        """내 PC에서 가장 무거운 프로세스 12개 (CPU/메모리 순)."""
        Tools._PC_TOKEN_REF[0] = self.valves.PCGATE_TOKEN
        Tools._PC_URL_REF[0] = self.valves.PCGATE_URL
        return await self._pc("procs")

    async def pc_updates(self) -> str:
        """내 PC의 보류 중인 Windows 업데이트 목록 확인."""
        Tools._PC_TOKEN_REF[0] = self.valves.PCGATE_TOKEN
        Tools._PC_URL_REF[0] = self.valves.PCGATE_URL
        return await self._pc("updates", timeout=90)

    async def find_files(self, path: str, pattern: str, max_depth: int = 4) -> str:
        """서버/디스크에서 파일·폴더를 이름으로 검색한다.
        - path: 검색 시작 경로 (예: "/mnt/external", "/home/hoony/models")
        - pattern: 파일명 패턴 (예: "*mimo*", "*.gguf")
        """
        return await self._call(
            "bash", {
                "command": f"find {path} -maxdepth {max_depth} -iname {pattern} 2>/dev/null | head -40",
            }, timeout=90
        )

    async def disk_usage(self, path: str = "/mnt/external") -> str:
        """디스크/폴더 용량 확인 (df 및 폴더별 크기)."""
        return await self._call(
            "bash", {
                "command": f"df -h {path} 2>/dev/null | tail -1; du -sh {path}/* 2>/dev/null | sort -rh | head -15",
            }, timeout=120
        )

    async def pc_say(self, text: str) -> str:
        """내 PC 스피커로 텍스트를 소리내어 말하게 한다 (한국어 음성).
        - text: 말할 문장
        """
        Tools._PC_TOKEN_REF[0] = self.valves.PCGATE_TOKEN
        Tools._PC_URL_REF[0] = self.valves.PCGATE_URL
        return await self._pc("speak", text=text, timeout=60)

    async def pc_action(self, action: str, confirm: bool = False) -> str:
        """내 PC 제어 (신중히 사용).
        - action: "lock"=화면잠금, "sleep"=절전, "restart"=재시작, "shutdown"=종료
        - confirm: restart/shutdown/sleep은 반드시 true로
        """
        Tools._PC_TOKEN_REF[0] = self.valves.PCGATE_TOKEN
        Tools._PC_URL_REF[0] = self.valves.PCGATE_URL
        if action in ("restart", "shutdown", "sleep") and not confirm:
            return f"[확인필요] {action} 은(는) 사용자 확인 후 confirm=true로 호출하세요."
        return await self._pc(action, confirm=True, timeout=20)