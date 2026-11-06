# -*- coding: utf-8 -*-
"""起售时间表 + 放票瞬间抢票调度。

为什么需要这个：12306 每天只在各站固定时刻放出「第 15 天」的票。
抢票的胜负 90% 决定于这一天放票的前后 3 秒，而不是整天匀速轮询。
py12306 / testerSunshine 都是匀速轮询，没有围绕起售时间做窗口调度。

数据来源（2026-11 实测可用）：
  GET https://kyfw.12306.cn/otn/index12306/queryAllCacheSaleTime
  → {"status":true,"data":[{"station_telecode":"VNP","station_name":"北京南",
                            "sale_time":"1245", ...}]}   共 3192 条
  注意 sale_time 是 HHMM 字符串，如 "1245" = 12:45。

本模块能力：
  * fetch()      拉全表并落 data/sale_time.json（默认 7 天有效）
  * of(code)     查某站起售时刻 "HH:MM"
  * next_open()  算出「下一个放票时刻」的 Unix 时间戳（含今天已过则明天）
  * open_window()判断给定日期是否属于「今天要放的那一天」
  * Arm         放票窗口调度器：提前 N 秒进入预热，放票瞬间切到突发模式
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .api.constants import BASE, HOST
from .config import DATA, paths
from .log import log

Log = log("sale")

SALE_TIME_URL = "https://kyfw.12306.cn/otn/index12306/queryAllCacheSaleTime"
CACHE_FILE = os.path.join(DATA, "sale_time.json")
CACHE_TTL = 7 * 86400

# 预售期：2026-11 实测 15 天（含当天）。放票时刻放出的是 today+14 那天。
PRESALE_DAYS = 15


# ---------------------------------------------------------------- 数据表
class SaleTimeBook:
    """站点起售时刻表。"""

    def __init__(self, session=None):
        self._session = session
        self._map: Dict[str, str] = {}      # telecode -> "HH:MM"
        self._name: Dict[str, str] = {}     # telecode -> 中文名
        self._loaded_at = 0.0
        self._loaded = False

    # ---------------- 加载
    def load(self, force: bool = False) -> "SaleTimeBook":
        if self._loaded and not force:
            return self
        if not force and os.path.exists(CACHE_FILE):
            try:
                raw = json.load(open(CACHE_FILE, encoding="utf-8"))
                if time.time() - float(raw.get("t", 0)) < CACHE_TTL:
                    self._apply(raw.get("rows") or [])
                    if self._map:
                        self._loaded = True
                        return self
            except Exception:
                pass
        self.fetch()
        return self

    def __len__(self) -> int:
        return len(self._map)

    def fetch(self) -> int:
        """拉全表，返回条数。失败保留旧数据。"""
        rows: List[dict] = []
        try:
            if self._session is not None:
                r = self._session.get(SALE_TIME_URL, use_cdn=False)
                d = r.json_data or {}
                rows = d.get("data") or []
            else:
                import httpx
                with httpx.Client(verify=False, http2=True, timeout=15,
                                  headers={"User-Agent": "Mozilla/5.0",
                                           "Referer": BASE + "/index/"}) as c:
                    rows = c.get(SALE_TIME_URL).json().get("data") or []
        except Exception as e:
            Log.warning("起售时间表拉取失败: %s", e)
        if not rows:
            return len(self._map)
        self._apply(rows)
        try:
            os.makedirs(DATA, exist_ok=True)
            json.dump({"t": time.time(), "rows": rows},
                      open(CACHE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass
        Log.info("起售时间表已更新 %d 站", len(self._map))
        return len(self._map)

    def _apply(self, rows: List[dict]) -> None:
        m: Dict[str, str] = {}
        n: Dict[str, str] = {}
        for r in rows:
            code = str(r.get("station_telecode") or "").upper()
            st = str(r.get("sale_time") or "")
            if not code or len(st) < 3:
                continue
            st = st.zfill(4)
            m[code] = f"{st[:2]}:{st[2:4]}"
            n[code] = str(r.get("station_name") or "")
        if m:
            self._map, self._name = m, n
            self._loaded_at = time.time()

    # ---------------- 查询
    def of(self, code: str) -> str:
        """返回 'HH:MM'；未知站返回 ''。"""
        self.load()
        return self._map.get(str(code or "").upper(), "")

    def name_of(self, code: str) -> str:
        self.load()
        return self._name.get(str(code or "").upper(), "")

    def minutes_of(self, code: str) -> Optional[int]:
        s = self.of(code)
        if not s:
            return None
        h, _, mi = s.partition(":")
        try:
            return int(h) * 60 + int(mi)
        except Exception:
            return None

    # ---------------- 放票时刻
    def next_open(self, code: str, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
        """下一个放票时刻（今天该时刻已过则顺延到明天）。"""
        mins = self.minutes_of(code)
        if mins is None:
            return None
        now = now or dt.datetime.now()
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=mins)
        if target <= now:
            target += dt.timedelta(days=1)
        return target

    def open_datetime(self, code: str, date: str,
                      now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
        """给定乘车日期，返回这张票的放票时刻。

        放票规律：乘车日期 D 的票，在 D-(PRESALE_DAYS-1) 那天的起售时刻放出。
        """
        mins = self.minutes_of(code)
        if mins is None:
            return None
        try:
            d = dt.date.fromisoformat(date)
        except Exception:
            return None
        launch_day = d - dt.timedelta(days=PRESALE_DAYS - 1)
        return dt.datetime.combine(launch_day, dt.time()) + dt.timedelta(minutes=mins)

    def open_soon(self, code: str, date: str, within: float = 3600,
                  now: Optional[dt.datetime] = None) -> Optional[float]:
        """返回距离开抢还有多少秒；不在窗口内返回 None。"""
        t = self.open_datetime(code, date, now)
        if not t:
            return None
        delta = (t - (now or dt.datetime.now())).total_seconds()
        return delta if -30 <= delta <= within else None


# ---------------------------------------------------------------- 放票窗口调度
@dataclass
class ArmPlan:
    """一次放票窗口的调度计划。"""
    open_at: float = 0.0          # Unix 时间戳
    warmup: float = 120.0         # 提前多少秒进入预热（低频探活）
    burst_before: float = 3.0     # 提前多少秒切突发
    burst_after: float = 60.0     # 放票后维持突发多少秒
    burst_interval: float = 0.25  # 突发期查询间隔
    burst_rate: float = 12.0      # 突发期限速（每秒请求数）

    def phase(self, now: Optional[float] = None) -> str:
        now = now or time.time()
        d = self.open_at - now
        if d > self.warmup:
            return "idle"
        if d > self.burst_before:
            return "warmup"
        if d > -self.burst_after:
            return "burst"
        return "done"

    def seconds_to_open(self, now: Optional[float] = None) -> float:
        return self.open_at - (now or time.time())


class Arm:
    """把「等到放票时刻再猛打」这件事封装起来。

    用法（worker 里）：
        arm = Arm.for_task(task, session)
        while ...:
            arm.wait_turn(stop_event)       # 阻塞到该查询的时刻
            trains = query.fetch(...)
            arm.tick()                      # 自适应调整间隔
    """

    def __init__(self, plan: ArmPlan, base_interval: float = 0.8):
        self.plan = plan
        self.base_interval = base_interval
        self._last = 0.0

    @classmethod
    def for_task(cls, task, session=None) -> Optional["Arm"]:
        """给任务算出最近的放票窗口；没有则返回 None。"""
        book = SaleTimeBook(session)
        code = getattr(task, "_from_code", "") or ""
        if not code:
            return None
        best: Optional[dt.datetime] = None
        for date in getattr(task, "resolved_dates", lambda: [])():
            t = book.open_datetime(code, date)
            if not t:
                continue
            delta = (t - dt.datetime.now()).total_seconds()
            if -60 <= delta <= 86400 * 2:
                if best is None or t < best:
                    best = t
        if best is None:
            return None
        plan = ArmPlan(open_at=best.timestamp())
        Log.info("检测到放票窗口 %s（还有 %.0f 秒）",
                 best.strftime("%m-%d %H:%M:%S"), plan.seconds_to_open())
        return cls(plan)

    # ---------------- 节奏控制
    def interval(self, now: Optional[float] = None) -> float:
        ph = self.plan.phase(now)
        if ph == "burst":
            return self.plan.burst_interval
        if ph == "warmup":
            return max(1.0, self.base_interval * 2)
        return max(self.base_interval, 3.0)

    def wait_turn(self, stop_event=None, now: Optional[float] = None) -> str:
        """睡到下一次该查询的时刻，返回当时的阶段。"""
        now = now or time.time()
        ph = self.plan.phase(now)
        # 距离放票还远：直接睡到预热开始（分片，可中断）
        left = self.plan.open_at - now - self.plan.warmup
        if ph == "idle" and left > 0:
            self._sleep(min(left, 30.0), stop_event)
            return "idle"
        gap = self.interval(now) - (time.time() - self._last)
        if gap > 0:
            self._sleep(gap, stop_event)
        self._last = time.time()
        return ph

    def tick(self) -> None:
        self._last = time.time()

    @staticmethod
    def _sleep(s: float, stop_event=None) -> None:
        end = time.time() + max(0.0, s)
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(0.2, max(0.0, end - time.time())))

    # ---------------- 限速联动
    def apply_rate(self) -> None:
        """突发期把全局令牌桶调快，结束后复位。"""
        try:
            from .net import limiter
            base = None
            try:
                from .config import config
                base = config().net.rate_per_sec
            except Exception:
                base = 4.0
            ph = self.plan.phase()
            limiter().retune(self.plan.burst_rate if ph == "burst" else (base or 4.0))
        except Exception:
            pass


# ---------------------------------------------------------------- 便捷函数
def sale_time(code: str, session=None) -> str:
    return SaleTimeBook(session).of(code)


def next_open(code: str, session=None) -> Optional[dt.datetime]:
    return SaleTimeBook(session).next_open(code)


def describe(task, session=None) -> str:
    """给 CLI/日志用的一句话摘要。"""
    book = SaleTimeBook(session)
    code = getattr(task, "_from_code", "") or ""
    st = book.of(code)
    if not st:
        return "起售时间未知"
    nxt = book.next_open(code)
    return f"{book.name_of(code) or code} 每日 {st} 起售，下个放票点 {nxt.strftime('%m-%d %H:%M')}"
