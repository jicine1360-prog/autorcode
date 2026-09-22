"""실제 실행 이벤트를 stderr에 표시한다. 모델의 원시 추론/JSON은 출력하지 않는다."""
import itertools
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager


def clean(text):
    # 웹/셸 결과가 터미널 제어 시퀀스로 진행 표시를 덮어쓰지 않도록 한다.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(text))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or (c.isprintable()))


def short(text, limit=160):
    text = " ".join(clean(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def terminal_line(text, width):
    output, used = [], 0
    for char in short(text, 400):
        cells = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
        if used + cells > width - 1:
            return "".join(output) + "…"
        output.append(char)
        used += cells
    return "".join(output)


def tool_label(name, args):
    if name == "bash":
        detail = "$ " + str(args.get("command", ""))
    elif name == "web_search":
        detail = args.get("query", "")
    elif name in ("youtube", "web_fetch"):
        detail = str(args.get("url", ""))
        if name == "youtube":
            detail += " · " + str(args.get("get", "info"))
    else:
        detail = str(args.get("path", "."))
        if name == "grep_files":
            detail += " · " + str(args.get("pattern", ""))
        if name == "write_file":
            detail += f" · {len(str(args.get('content', ''))):,}자 쓰기"
        if name == "edit_file":
            detail += " · 부분 치환"
    return short(f"{name} · {detail}")


def result_status(result):
    prefix = str(result).lstrip()
    if prefix.startswith("[오류]"):
        if "거부" in prefix[:150]:
            return "거부"
        if "타임아웃" in prefix[:150]:
            return "시간초과"
        return "실패"
    match = re.match(r"\[exit=(-?\d+)\]", prefix)
    if match and int(match.group(1)) != 0:
        return "시간초과" if "[TIMEOUT]" in prefix else "실패"
    return "완료"


class Activity:
    def __init__(self, label):
        self.label = short(label)
        self.started = time.monotonic()
        self.message = ""

    @property
    def elapsed(self):
        return time.monotonic() - self.started


class Progress:
    def __init__(self, enabled=True, details=False, stream=None, interval=5.0):
        self.enabled = enabled
        self.details = details
        self.stream = stream if stream is not None else sys.stderr
        self.interval = max(0.05, interval)
        self.tty = self.stream.isatty() and os.getenv("TERM") != "dumb"
        self._lock = threading.RLock()
        self._line_visible = False

    def _clear(self):
        if self._line_visible:
            self.stream.write("\r\x1b[2K")
            self._line_visible = False

    def event(self, text):
        if not self.enabled:
            return
        with self._lock:
            self._clear()
            self.stream.write(clean(text) + "\n")
            self.stream.flush()

    def update(self, activity, message):
        with self._lock:
            activity.message = short(message)

    def _heartbeat(self, activity, stop):
        frames = itertools.cycle("|/-\\")
        interval = 0.15 if self.tty else self.interval
        while not stop.wait(interval):
            with self._lock:
                label = activity.message or activity.label
                if self.tty:
                    self._clear()
                    width = max(20, shutil.get_terminal_size((100, 24)).columns - 2)
                    self.stream.write("\r" + terminal_line(
                        f"  {next(frames)} {label} · {activity.elapsed:.1f}s", width))
                    self._line_visible = True
                    self.stream.flush()
                else:
                    self.event(f"  [진행] {label} · {activity.elapsed:.1f}s")

    @contextmanager
    def activity(self, label):
        activity = Activity(label)
        stop = threading.Event()
        worker = None
        self.event(label)
        if self.enabled:
            worker = threading.Thread(target=self._heartbeat, args=(activity, stop),
                                      name="autorcode-progress", daemon=True)
            worker.start()
        try:
            yield activity
        finally:
            stop.set()
            if worker:
                worker.join()
            with self._lock:
                self._clear()
                if self.enabled:
                    self.stream.flush()

    def result(self, label, result, elapsed):
        if not self.enabled:
            return
        self.event(f"  [{result_status(result)}] {short(label)} · {elapsed:.1f}s · {len(result):,}자")
        lines = [line.strip() for line in clean(result).splitlines() if line.strip()]
        count = 12 if self.details else 3
        for line in lines[:count]:
            self.event("    │ " + short(line, 200 if self.details else 140))
        if len(lines) > count:
            self.event(f"    └ … {len(lines) - count}줄 더 있음" +
                       ("" if self.details else " (--details로 더 보기)"))
