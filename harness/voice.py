"""음성→텍스트(STT) — openai-whisper CLI 래퍼 (기본 로컬, 폰 소리가 밖으로 안 나감).

- AUTORCODE_WHISPER_BIN: whisper 실행파일 경로(기본 ~/.local/bin/whisper → PATH)
- AUTORCODE_STT_MODEL   : base(기본·빠름) | small | medium | large-v3-turbo(고품질)
- runner 로 실행을 주입할 수 있어 테스트는 whisper 없이 도는 구조.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Callable, Optional

log = logging.getLogger("agent.voice")

DEFAULT_MODEL = os.getenv("AUTORCODE_STT_MODEL", "base")


def _bin() -> Optional[str]:
    env = os.getenv("AUTORCODE_WHISPER_BIN", "").strip()
    if env and os.path.isfile(os.path.expanduser(env)):
        return os.path.expanduser(env)
    found = shutil.which("whisper")
    if found:
        return found
    home = os.path.expanduser("~/.local/bin/whisper")
    return home if os.path.isfile(home) else None


def _run_env() -> dict:
    """systemd 유닛의 PATH에는 ~/bin 이 없을 수 있다 — ffmpeg/whisper 위치를 보강한다."""
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep)
    for d in (os.path.expanduser("~/bin"), os.path.expanduser("~/.local/bin"),
              "/usr/bin", "/usr/local/bin"):
        if os.path.isdir(d) and d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _default_runner(argv, timeout):
    r = subprocess.run(argv, capture_output=True, text=True,
                       timeout=timeout, env=_run_env())
    return r.returncode, (r.stderr or "")


def transcribe(path: str, model: str = "", timeout: int = 180,
               runner: Optional[Callable] = None) -> str:
    """음성 파일(ogg/wav/mp3...) → 인식 텍스트. 실패는 '[오류] ...' 문자열."""
    audio = os.path.expanduser(str(path or "").strip())
    if not os.path.isfile(audio):
        return f"[오류] 음성 파일을 찾지 못했습니다: {audio}"
    whisper = _bin()
    if not whisper:
        return ("[오류] whisper CLI 가 없어요 — pip install openai-whisper 후 "
                "AUTORCODE_WHISPER_BIN 설정")
    model = (model or DEFAULT_MODEL).strip()
    outdir = tempfile.mkdtemp(prefix="autorcode-stt-")
    stem = os.path.splitext(os.path.basename(audio))[0]
    txt = os.path.join(outdir, stem + ".txt")
    argv = [whisper, audio, "--model", model, "--output_format", "txt",
            "--output_dir", outdir]
    try:
        rc, err = (runner or _default_runner)(argv, timeout)
    except subprocess.TimeoutExpired:
        return f"[오류] 음성 인식 타임아웃({timeout}초)"
    except Exception as e:
        return f"[오류] whisper 실행 실패: {type(e).__name__}: {e}"
    finally:
        pass
    try:
        if rc != 0:
            return f"[오류] whisper rc={rc}: {err[:200]}"
        if not os.path.isfile(txt):
            found = glob.glob(os.path.join(outdir, "*.txt"))
            if not found:
                return "[오류] 인식 결과 파일이 없음"
            txt = found[0]
        with open(txt, encoding="utf-8") as f:
            return f.read().strip()
    finally:
        shutil.rmtree(outdir, ignore_errors=True)