# -*- coding: utf-8 -*-
"""日志：彩色终端 + 环形缓冲（给 Web 用）+ 文件。

不依赖第三方库，Windows 终端自动开 ANSI。
"""
from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time
from typing import Deque, Dict, List, Optional

from .config import paths

OK = 25
HIT = 35
logging.addLevelName(OK, "OK")
logging.addLevelName(HIT, "HIT")

_COLORS = {
    "DEBUG": "\033[90m", "INFO": "\033[36m", "OK": "\033[32m",
    "WARNING": "\033[33m", "ERROR": "\033[31m", "HIT": "\033[1;35m",
}
_RESET = "\033[0m"

_RING: Deque[Dict] = collections.deque(maxlen=4000)
_RING_LOCK = threading.Lock()
_READY = False


def _enable_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            with _RING_LOCK:
                _RING.append({
                    "t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "ts": record.created,
                    "level": record.levelname,
                    "tag": getattr(record, "tag", "app"),
                    "msg": record.getMessage(),
                })
        except Exception:
            pass


class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = _COLORS.get(record.levelname, "")
        tag = getattr(record, "tag", "app")
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        body = record.getMessage()
        line = f"{color}{ts} [{record.levelname:<7}]{_RESET} {tag:<9} {body}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(level: str = "INFO", to_file: bool = True):
    global _READY
    if _READY:
        return
    _enable_ansi()
    root = logging.getLogger("ts")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(ColorFormatter())
    sh.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.addHandler(sh)

    if to_file:
        try:
            os.makedirs(paths.LOGS, exist_ok=True)
            fh = logging.FileHandler(os.path.join(paths.LOGS, "ticketsniper.log"), encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
            fh.setLevel(getattr(logging, str(level).upper(), logging.INFO))
            root.addHandler(fh)
        except Exception:
            pass

    root.addHandler(RingHandler())
    _READY = True


def set_level(level: str):
    lv = getattr(logging, str(level).upper(), logging.INFO)
    for h in logging.getLogger("ts").handlers:
        if not isinstance(h, RingHandler):
            h.setLevel(lv)


class Tagged:
    """带 tag 的日志门面。"""

    def __init__(self, tag: str):
        self.tag = tag

    def _log(self, level: int, msg, *a, **kw):
        extra = dict(kw.pop("extra", {}) or {})
        extra.setdefault("tag", self.tag)
        logging.getLogger("ts").log(level, msg, *a, extra=extra, **kw)

    def debug(self, msg, *a, **kw):
        self._log(logging.DEBUG, msg, *a, **kw)

    def info(self, msg, *a, **kw):
        self._log(logging.INFO, msg, *a, **kw)

    def ok(self, msg, *a, **kw):
        self._log(OK, msg, *a, **kw)

    def hit(self, msg, *a, **kw):
        self._log(HIT, msg, *a, **kw)

    def warn(self, msg, *a, **kw):
        self._log(logging.WARNING, msg, *a, **kw)

    warning = warn

    def error(self, msg, *a, **kw):
        self._log(logging.ERROR, msg, *a, **kw)

    def exc(self, msg, *a, **kw):
        kw["exc_info"] = True
        self._log(logging.ERROR, msg, *a, **kw)


def log(tag: str) -> Tagged:
    return Tagged(tag)


def recent(n: int = 200, level: Optional[str] = None, tag: Optional[str] = None,
           since_ts: float = 0.0, keyword: str = "") -> List[Dict]:
    with _RING_LOCK:
        items = list(_RING)
    if level:
        items = [x for x in items if x["level"].upper() == level.upper()]
    if tag:
        items = [x for x in items if x["tag"] == tag]
    if since_ts:
        items = [x for x in items if x["ts"] > since_ts]
    if keyword:
        items = [x for x in items if keyword in x["msg"]]
    return items[-n:]
