"""환경설정 — OpenAI호환 또는 로컬 ollama 프리셋. 용량이 없으면 mock."""
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field

# 실제 ollama에 설치된 모델명이어야 한다. 없는 이름을 넣으면 조용히 pull로
# 시도하다 404에 죽거나, 조용히 모크로 떨어진다. ollama list 로 확인 후 바꿔라.
_OLLAMA_PRESET = {
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "ollama",
    # qwen3:30b-a3b = 30.5B MoE(활성 3B) 저지연 fast 티어 / qwen3.8:latest = 27.3B 지능 우선 smart
    "model_fast": os.getenv("AGENT_MODEL_FAST", "qwen3:30b-a3b"),
    "model_smart": os.getenv("AGENT_MODEL_SMART", "qwen3.8:latest"),
}


@dataclass
class Config:
    # --- LLM 백엔드 ---
    base_url: str = os.getenv("AGENT_BASE_URL", "")
    api_key: str = os.getenv("AGENT_API_KEY", "")
    model_fast: str = os.getenv("AGENT_MODEL_FAST", "gpt-4o-mini")
    model_smart: str = os.getenv("AGENT_MODEL_SMART", "gpt-4o")
    temperature: float = float(os.getenv("AGENT_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("AGENT_MAX_TOKENS", "8192"))
    api_timeout: int = int(os.getenv("AGENT_API_TIMEOUT", "120"))
    api_retries: int = int(os.getenv("AGENT_API_RETRIES", "3"))

    # --- 루프 가드 ---
    max_iterations: int = int(os.getenv("AGENT_MAX_STEPS", "15"))

    # --- 도구 하네스 한도 ---
    bash_timeout: int = int(os.getenv("AGENT_BASH_TIMEOUT", "30"))
    max_output: int = int(os.getenv("AGENT_MAX_OUTPUT", "8000"))
    max_actions: int = int(os.getenv("AGENT_MAX_ACTIONS", "4"))

    # --- 컨텍스트 예산(토큰 기준 추정) ---
    context_tokens: int = int(os.getenv("AGENT_CONTEXT_TOKENS", "40000"))
    memory_file: str = os.getenv("AGENT_MEMORY_FILE", "")
    memory_max_chars: int = int(os.getenv("AGENT_MEMORY_MAX_CHARS", "3000"))

    # --- 권한: yolo | balanced | strict ---
    permissions_mode: str = os.getenv("AGENT_PERMS", "balanced")
    auto_yes: bool = False  # CLI --yes
    auto_smart: bool = os.getenv("AGENT_AUTO", "0") == "1"  # CLI --auto — 파괴 명령만 승인
    show_steps: bool = os.getenv("AGENT_SHOW_STEPS", "1") == "1"  # 과정 실시간 출력
    show_details: bool = os.getenv("AGENT_SHOW_DETAILS", "0") == "1"
    stream: bool = os.getenv("AGENT_STREAM", "1") == "1"
    native_tools: bool = os.getenv("AGENT_NATIVE_TOOLS", "1") == "1"  # 네이티브 function calling

    # --- 프로세스 리소스 한도(비특권 하네스) ---
    rlimit_cpu: int = int(os.getenv("AGENT_RLIMIT_CPU", "60"))
    rlimit_mem_mb: int = int(os.getenv("AGENT_RLIMIT_MEM_MB", "4096"))
    rlimit_fsize_mb: int = int(os.getenv("AGENT_RLIMIT_FSIZE_MB", "64"))
    rlimit_nproc: int = int(os.getenv("AGENT_RLIMIT_NPROC", "128"))

    # --- 세션 ---
    session_file: str = os.getenv("AGENT_SESSION", "")

    workspace_root: str = field(
        default_factory=lambda: os.path.realpath(os.getenv("AGENT_WORKSPACE", os.getcwd())))
    log_file: str = os.getenv("AGENT_LOG", "agent.log")

    @property
    def use_mock(self) -> bool:
        return not self.base_url


def _ollama_base(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def installed_models(base_url: str, timeout: int = 5) -> list[str] | None:
    """ollama에 pull된 모델명 목록. 서버에 닿지 않으면 None."""
    try:
        req = urllib.request.Request(f"{_ollama_base(base_url)}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return [m.get("name") for m in json.loads(resp.read()).get("models", [])]
    except Exception:
        return None


def preflight(cfg: Config) -> list[str]:
    """구동 전에 반드시 봐야 할 경고. 조용히 모크/오타로 떨어지는 것을 막는다."""
    if cfg.use_mock:
        return [
            "[모크 모드] AGENT_BASE_URL 이 비어 있어 규칙 기반 더미로 돕니다. "
            "실제 모델로 돌리려면: --provider ollama 또는 "
            "AGENT_PROVIDER=ollama (또는 AGENT_BASE_URL/AGENT_API_KEY 지정)"
        ]
    if "127.0.0.1" not in cfg.base_url and "localhost" not in cfg.base_url:
        return []
    names = installed_models(cfg.base_url)
    if names is None:
        return [f"[경고] {cfg.base_url} 에 닿지 않습니다 — ollama 서버가 살아있는지 확인하세요"]
    known = set(names) | {n.split(":", 1)[0] for n in names}
    missing = [m for m in (cfg.model_fast, cfg.model_smart) if m and m not in known]
    if missing:
        return [
            f"[오류] 설정된 모델이 설치돼 있지 않습니다: {', '.join(missing)} — "
            f"ollama pull 하거나 AGENT_MODEL_FAST/AGENT_MODEL_SMART 를 바꾸세요. "
            f"설치됨: {', '.join(names) or '(없음)'}"
        ]
    return []


def load() -> Config:
    cfg = Config()
    provider = os.getenv("AGENT_PROVIDER", "").lower()
    if provider == "ollama":
        cfg.base_url = cfg.base_url or _OLLAMA_PRESET["base_url"]
        cfg.api_key = cfg.api_key or _OLLAMA_PRESET["api_key"]
        if cfg.model_fast == "gpt-4o-mini":
            cfg.model_fast = _OLLAMA_PRESET["model_fast"]
        if cfg.model_smart == "gpt-4o":
            cfg.model_smart = _OLLAMA_PRESET["model_smart"]
    return cfg


def setup_logging(verbose: bool = False) -> None:
    import logging

    root = logging.getLogger("agent")
    root.setLevel(logging.DEBUG)
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stderr)  # 사용자 stdout 안 더럽히기
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(fmt)
    fh = logging.FileHandler(load().log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(fh)
