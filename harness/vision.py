"""사진 이해(VLM) — 이미지의 내용을 질문과 함께 설명한다.

기본은 로컬 ollama vision 모델(폰 사진이 클라우드로 안 나감):
  AUTORCODE_VISION_BASE_URL (기본 http://127.0.0.1:11434/v1)
  AUTORCODE_VISION_MODEL    (기본 qwen2.5vl:3b)
  AUTORCODE_VISION_KEY      (클라우드 사용 시 Bearer)

테스트는 vision._post 를 스텁으로 교체해 네트워크 없이 돈다.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request

log = logging.getLogger("agent.vision")

BASE_URL = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5vl:3b"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp"}


def _cfg():
    return (os.getenv("AUTORCODE_VISION_BASE_URL", BASE_URL).rstrip("/"),
            os.getenv("AUTORCODE_VISION_MODEL", MODEL),
            os.getenv("AUTORCODE_VISION_KEY", "").strip())


def _post(url: str, payload: dict, key: str, timeout: int):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def describe(path: str, question: str = "", timeout: int = 90) -> str:
    """이미지 파일 → VLM 응답 텍스트. 실패는 '[오류] ...' 문자열로 돌려준다."""
    base, model, key = _cfg()
    p = os.path.expanduser(str(path or "").strip())
    if not p:
        return "[오류] 이미지 경로가 비어 있다"
    if not os.path.isfile(p):
        return f"[오류] 파일을 찾지 못했습니다: {p}"
    ext = os.path.splitext(p)[1].lower()
    mime = MIME.get(ext)
    if not mime:
        return f"[오류] 지원하지 않는 이미지 형식: {ext or '(없음)'} (jpg/png/gif/webp)"
    size = os.path.getsize(p)
    if size > MAX_IMAGE_BYTES:
        return f"[오류] 이미지가 큽니다 ({size // 1024 // 1024}MB, 한도 8MB)"
    with open(p, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    text = (question.strip() or "이 사진에 무엇을 보이는 대로 한국어로 설명해줘.")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
        "max_tokens": 700,
        "temperature": 0.2,
    }
    try:
        data = _post(f"{base}/chat/completions", payload, key, timeout)
    except Exception as e:
        log.warning("VLM 호출 실패 (%s): %s", base, e)
        return (f"[오류] vision 모델 응답 실패: {type(e).__name__}: {str(e)[:120]} "
                f"(AUTORCODE_VISION_MODEL={model!r} 이 ollama 에 설치됐는지 확인)")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return f"[오류] 응답 형식 이상: {json.dumps(data, ensure_ascii=False)[:200]}"
    if isinstance(content, list):  # 일부 서버는 파트 배열로 준다
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    content = (content or "").strip()
    return content or "[오류] 빈 응답"
