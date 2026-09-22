"""환경설정 — OpenAI호환 또는 로컬 ollama 프리셋. 용량이 없으면 mock."""
import os
import sys
from dataclasses import dataclass, field

_OLLAMA_PRESET = {
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "ollama",
    # phi4(9GB)=GPU에서 바로 도는 저지연 fast 티어 / qwen3.8 27b=지능 우선 smart(한자 CPU바운드라 느림)
    "model_fast": os.getenv("AGENT_MODEL_FAST", "phi4:latest"),
    "model_smart": os.getenv("AGENT_MODEL_SMART", "qwen3.8:27b-hunmin-64k"),
}


@dataclass
class Config:
    # --- LLM 백엔드 ---
    base_url: str = os.getenv("AGENT_BASE_URL", "")
    api_key: str = os.getenv("AGENT_API_KEY", "")
    model_fast: str = os.getenv("AGENT_MODEL_FAST", "gpt-4o-mini")
    model_smart: str = os.getenv("AGENT_MODEL_SMART", "gpt-4o")
    temperature: float = float(os.getenv("AGENT_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("AGENT_MAX_TOKENS", "800"))
    api_timeout: int = int(os.getenv("AGENT_API_TIMEOUT", "120"))
    api_retries: int = int(os.getenv("AGENT_API_RETRIES", "3"))

    # --- 루프 가드 ---
    max_iterations: int = int(os.getenv("AGENT_MAX_STEPS", "15"))

    # --- 도구 하네스 한도 ---
    bash_timeout: int = int(os.getenv("AGENT_BASH_TIMEOUT", "30"))
    max_output: int = int(os.getenv("AGENT_MAX_OUTPUT", "8000"))
    max_actions: int = int(os.getenv("AGENT_MAX_ACTIONS", "4"))

    # --- 컨텍스트 예산(토큰 기준 추정) ---
    context_tokens: int = int(os.getenv("AGENT_CONTEXT_TOKENS", "20000"))

    # --- 권한: yolo | balanced | strict ---
    permissions_mode: str = os.getenv("AGENT_PERMS", "balanced")
    auto_yes: bool = False  # CLI --yes
    show_steps: bool = os.getenv("AGENT_SHOW_STEPS", "1") == "1"  # 과정 실시간 출력
    show_details: bool = os.getenv("AGENT_SHOW_DETAILS", "0") == "1"
    stream: bool = os.getenv("AGENT_STREAM", "1") == "1"

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
