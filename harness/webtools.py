"""웹 도구 — 검색(DDG lite, 키 없음), 페이지 읽기, 유튜브(yt-dlp).

보안: SSRF 가드(사설 IP/루프백 거부), 사이즈/타임아웃 상한, HTML→텍스트 정제.
yt-dlp는 시스템에 설치된 바이너리만 사용하고 자막파일은 샌드박스 안에 쓴다.
"""
import hashlib
import html as _html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_PRIVATE = re.compile(
    r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|0\.|::1|localhost)", re.I)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[상한 {limit}자 초과, {len(text) - limit}자 생략]"


def _public_host(url: str) -> str:
    """SSRF 가드 — http(s)만, 호스트가 사설/루프백으로 풀리면 거부."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"지원 안 하는 URL: {url!r} (http/https만)")
    for family, _, _, _, sa in socket.getaddrinfo(p.hostname, p.port or 443):
        ip = sa[0]
        if _PRIVATE.match(ip):
            raise ValueError(f"사설/내부 주소 거부: {p.hostname} → {ip}")
    return url


def _get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(_public_host(url), headers={
        "User-Agent": _UA, "Accept-Language": "ko,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(1_500_000)
        final = r.geturl()
    _public_host(final)  # 리다이렉트가 내부로 튀었는지 재검사
    return data


def _html_to_text(raw: bytes) -> str:
    txt = raw.decode("utf-8", "replace")
    txt = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", txt)
    txt = re.sub(r"(?s)<!--.*?-->", " ", txt)
    txt = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt.strip()


# ---------------- DuckDuckGo 검색 (두 레이아웃 모두 지원) ----------------

def _ddg_parse(raw: bytes) -> list:
    txt = raw.decode("utf-8", "replace")
    out = []
    # lite 레이아웃: //duckduckgo.com/l/?uddg=<enc>&rut=... > 제목
    for m in re.finditer(r'uddg=([^&"\']+)[^>]*>(.*?)</a>', txt, re.S):
        url = urllib.parse.unquote(m.group(1))
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if title and url.startswith("http"):
            out.append({"title": title[:120], "url": url})
    if not out:  # html 레이아웃: class="result__a" href="..."
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', txt, re.S):
            url = urllib.parse.unquote(
                re.search(r"uddg=([^&]+)", m.group(1)).group(1)) if "uddg=" in m.group(1) else m.group(1)
            title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if title and url.startswith("http"):
                out.append({"title": title[:120], "url": url})
    # 중복 제거
    seen, uniq = set(), []
    for r in out:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    return uniq


def _web_search(args, root, max_output, timeout):
    query = str(args.get("query", "")).strip()
    if not query:
        return "[오류] query 필수"
    limit = int(args.get("max_results") or 8)
    tmo = min(timeout, 20)
    last_err = None
    for base in ("https://lite.duckduckgo.com/lite/?q=", "https://html.duckduckgo.com/html/?q="):
        try:
            raw = _get(base + urllib.parse.quote(query), tmo)
            results = _ddg_parse(raw)[:limit]
            if results:
                lines = [f"{i+1}. {r['title']}\n   {r['url']}" for i, r in enumerate(results)]
                return _cap("\n".join(lines), max_output)
            last_err = "결과 파싱 0건 (레이아웃 변화?)"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return f"[오류] 검색 실패 ({last_err}) — 잠시 후 재시도하거나 키워드를 바꿔라"


def _web_fetch(args, root, max_output, timeout):
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return "[오류] http(s) URL만 지원"
    tmo = min(timeout, 25)
    try:
        raw = _get(url, tmo)
    except Exception as e:
        return f"[오류] 가져오기 실패: {type(e).__name__}: {e}"
    text = raw.decode("utf-8", "replace") if url.lower().endswith((".txt", ".json", ".md")) \
        else _html_to_text(raw)
    head = f"URL: {url}\n{'-' * 40}\n"
    return _cap(head + text, max_output)


# ---------------- YouTube (yt-dlp: 메타+자막 = '보기') ----------------

def _youtube(args, root, max_output, timeout):
    url = str(args.get("url", "")).strip()
    mode = str(args.get("get", "info"))  # info | transcript
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return "[오류] yt-dlp 미설치 — pip install yt-dlp"
    if not re.match(r"https?://(www\.|m\.)?(youtube\.com/|youtu\.be/)", url):
        return "[오류] youtube URL만 지원 (검색은 web_search tool 사용)"
    tmo = min(timeout, 90)
    try:
        if mode == "info":
            out = subprocess.run([ytdlp, "-J", "--no-warnings", "--skip-download", url],
                                 capture_output=True, text=True, timeout=tmo)
            if out.returncode != 0:
                return f"[오류] yt-dlp: {(out.stderr or 'unknown')[:200]}"
            meta = json.loads(out.stdout)
            desc = (meta.get("description") or "")[:800]
            return _cap(
                f"제목: {meta.get('title')}\n채널: {meta.get('uploader')}"
                f"\n길이: {meta.get('duration_string')}\n조회수: {meta.get('view_count')}"
                f"\n업로드: {meta.get('upload_date')}\n\n설명:\n{desc}", max_output)
        # transcript
        tmpdir = os.path.join(root, ".autorcode_tmp")
        os.makedirs(tmpdir, exist_ok=True)
        template = os.path.join(tmpdir, "yt_" + _yt_cache_key(url) + ".%(ext)s")
        subprocess.run([ytdlp, "--skip-download", "--write-auto-subs", "--write-subs",
                        "--sub-langs", "ko,en", "-o", template, url],
                       capture_output=True, text=True, timeout=tmo)
        vtts = [f for f in os.listdir(tmpdir) if f.endswith(".vtt")]
        if not vtts:
            return "[오류] 자막 없음 (ko/en 자막이 없는 영상) — 'get':'info'로 메타만 시도"
        txt = open(os.path.join(tmpdir, vtts[0]), encoding="utf-8", errors="replace").read()
        txt = re.sub(r"(?m)^(WEBVTT|Kind:.*|Language:.*|NOTE.*)$", "", txt)
        txt = re.sub(r"\d{1,2}:\d{2}:\d{2}\.\d{3} --> .*", "", txt)
        txt = re.sub(r"<[^>]+>", "", txt)
        lines, prev = [], None
        for ln in (l.strip() for l in txt.splitlines()):
            if ln and ln != prev:
                lines.append(ln)
            prev = ln
        return _cap(f"[자막] {vtts[0]}\n" + "\n".join(lines), max_output)
    except subprocess.TimeoutExpired:
        return f"[오류] {tmo}초 타임아웃 — 유튜브 추출 실패"
    except Exception as e:
        return f"[오류] {type(e).__name__}: {e}"


def _yt_cache_key(url: str) -> str:
    """프로세스 무관하게 안정적인 유튜브 캐시 파일명 (python hash()는 실행마다 달라짐)"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def cleanup_tmp(root: str) -> None:
    shutil.rmtree(os.path.join(root, ".autorcode_tmp"), ignore_errors=True)


TOOLS = {
    "web_search": _web_search,
    "web_fetch": _web_fetch,
    "youtube": _youtube,
}

SCHEMAS = {
    "web_search": "args: {query:str, max_results?:int} — 웹 검색(DDG, 키 불필요)",
    "web_fetch": "args: {url:str} — 웹페이지 가져와 본문 텍스트로",
    "youtube": "args: {url:str, get?:'info'|'transcript'} — 영상 메타/자막 읽기",
}
