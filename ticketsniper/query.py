# -*- coding: utf-8 -*-
"""余票查询：解析 + 候选筛选。

老项目的通病是「只认 33 段固定下标」，12306 已扩到 59 段，
且 queryZ/queryA/queryG/queryO 四个入口返回的字段个数不同。
这里用名字索引 + 越界安全访问，字段缺失不炸。
"""
from __future__ import annotations

import datetime as dt
import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .api.constants import (CAN_BUY, F, NOT_BUY, QUERY_PARAMS, QUERY_ROUTES,
                            SEATS, Seat, URLS, parse_count, seat_of)
from .api.station import book
from .config import TaskConfig, config
from .log import log
from .net import Resp, Session, classify

Log = log("query")


# ---------------------------------------------------------------- 数据模型
@dataclass
class Train:
    secret_str: str
    train_no: str
    code: str                 # G531
    from_code: str
    to_code: str
    from_name: str
    to_name: str
    start_time: str
    arrive_time: str
    duration: str
    can_buy: str
    date: str                 # 2026-11-09
    train_date_raw: str       # 20261109
    location: str
    yp_info: str
    seat_types: str
    from_no: str = ""
    to_no: str = ""
    yp_detail: str = ""
    raw: List[str] = field(default_factory=list)
    # 席别 code -> 余票数（9999 = 有）
    counts: Dict[str, int] = field(default_factory=dict)
    query_at: float = 0.0

    @property
    def buyable(self) -> bool:
        return self.can_buy in CAN_BUY

    @property
    def not_open(self) -> bool:
        return self.can_buy in NOT_BUY or "NOT_BUY" in (self.can_buy or "")

    def count(self, seat_code: str) -> int:
        return self.counts.get(seat_code, 0)

    def seats_available(self) -> Dict[str, int]:
        return {k: v for k, v in self.counts.items() if v > 0}

    def label(self) -> str:
        return f"{self.code} {self.start_time}-{self.arrive_time}"

    def to_dict(self) -> dict:
        return {
            "code": self.code, "train_no": self.train_no,
            "from": self.from_name, "to": self.to_name,
            "start": self.start_time, "arrive": self.arrive_time,
            "duration": self.duration, "date": self.date,
            "can_buy": self.can_buy,
            "seats": {SEATS[k].name if k in SEATS else k: v for k, v in self.counts.items() if v > 0},
        }


def _at(parts: Sequence[str], idx: int, default: str = "") -> str:
    return parts[idx] if idx < len(parts) else default


def parse_line(line: str, date: str) -> Optional[Train]:
    """把一条 | 分隔的余票记录解析成 Train。"""
    try:
        parts = urllib.parse.unquote(line).split("|")
    except Exception:
        return None
    if len(parts) < 34:
        return None
    code = _at(parts, F["train_code"])
    if not code:
        return None
    counts: Dict[str, int] = {}
    for sc, seat in SEATS.items():
        v = parse_count(_at(parts, seat.field))
        if v:
            counts[sc] = v
    t = Train(
        secret_str=_at(parts, F["secret_str"]),
        train_no=_at(parts, F["train_no"]),
        code=code,
        from_code=_at(parts, F["from_station"]),
        to_code=_at(parts, F["to_station"]),
        from_name=book.name(_at(parts, F["from_station"])),
        to_name=book.name(_at(parts, F["to_station"])),
        start_time=_at(parts, F["start_time"]),
        arrive_time=_at(parts, F["arrive_time"]),
        duration=_at(parts, F["duration"]),
        can_buy=_at(parts, F["can_buy"]),
        date=date,
        train_date_raw=_at(parts, F["train_date"]),
        location=_at(parts, F["location"]),
        yp_info=_at(parts, F["yp_info"]),
        seat_types=_at(parts, F["seat_types"]),
        from_no=_at(parts, F["from_no"]),
        to_no=_at(parts, F["to_no"]),
        yp_detail=_at(parts, F["yp_detail"]),
        raw=parts,
        counts=counts,
        query_at=time.time(),
    )
    return t


# ---------------------------------------------------------------- 查询器
class QueryError(Exception):
    pass


class Query:
    """单 session 的余票查询。多 worker 时每个 worker 一个实例。"""

    def __init__(self, session: Session):
        self.s = session
        self._lock = threading.Lock()
        self.last_resp: Optional[Resp] = None
        self.stats = {"q": 0, "ok": 0, "fail": 0, "empty": 0}
        self.routes = list(QUERY_ROUTES)
        self._route_ok: Dict[str, int] = {r: 0 for r in self.routes}

    # ---------------- 单次
    def fetch(self, date: str, from_code: str, to_code: str,
              timeout: float = None) -> List[Train]:
        """一次查询；内部对四个路由做快速失败轮转。

        12306 的入口是动态的：请求 queryZ 可能返回
        {"c_url":"leftTicket/queryG","c_name":"CLeftTicketUrl","status":false}
        这是「路由已迁移」提示，必须按其指示改走 queryG，否则拿到的是错误页。
        """
        last_err = ""
        order = sorted(self.routes, key=lambda r: -self._route_ok.get(r, 0))
        tried = set()
        for _ in range(6):
            route = order.pop(0) if order else None
            if route is None or route in tried:
                break
            tried.add(route)
            url = URLS["query"].format(route=route)
            params = {
                "leftTicketDTO.train_date": date,
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                **QUERY_PARAMS,
            }
            # 余票接口一律直连：实测 CDN edge 对它返回 200 + HTML 错误页
            # （1904 字节首页片段），只有直连才是 JSON。
            r = self.s.get(url + "?" + urllib.parse.urlencode(params), use_cdn=False)
            self.stats["q"] += 1
            self.last_resp = r
            if not r.ok:
                last_err = r.error or f"HTTP {r.status}"
                continue
            jd = r.json_data
            if not isinstance(jd, dict):
                # 返回 HTML：先 init 预热拿 cookie，再重试一次
                self.s.preface()
                last_err = "非 JSON 响应（可能被风控或路由失效）"
                continue
            # 路由迁移提示
            if jd.get("c_url"):
                new_route = str(jd["c_url"]).rsplit("/", 1)[-1]
                if new_route and new_route not in tried:
                    self.routes = [new_route] + [x for x in QUERY_ROUTES if x != new_route]
                    order.insert(0, new_route)
                last_err = f"路由迁移 → {new_route}"
                continue
            if jd.get("httpstatus") not in (200, "200", None):
                last_err = f"httpstatus={jd.get('httpstatus')} {jd.get('messages')}"
                continue
            data = jd.get("data") or {}
            rows = data.get("result")
            if rows is None:
                last_err = f"无 result 字段: {str(jd)[:120]}"
                continue
            self._route_ok[route] = self._route_ok.get(route, 0) + 1
            if not rows:
                self.stats["empty"] += 1
                return []
            trains = [t for t in (parse_line(x, date) for x in rows) if t]
            self.stats["ok"] += 1
            return trains
        self.stats["fail"] += 1
        raise QueryError(last_err or "查询失败")

    def probe(self) -> bool:
        """健康检查：随便查一条已知线路，验证 session 可用。"""
        d = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        try:
            self.fetch(d, "BJP", "SHH")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------- 筛选
def _hm(s: str) -> int:
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def _in_window(t: str, window: str) -> bool:
    if not window or "-" not in window:
        return True
    a, b = window.split("-", 1)
    x, lo, hi = _hm(t), _hm(a.strip()), _hm(b.strip())
    if x < 0 or lo < 0 or hi < 0:
        return True
    if lo <= hi:
        return lo <= x <= hi
    # 跨零点，如 22:00-02:00
    return x >= lo or x <= hi


@dataclass
class Candidate:
    train: Train
    seat: Seat
    date: str

    @property
    def key(self) -> str:
        return f"{self.date}|{self.train.code}|{self.seat.code}"

    def __str__(self):
        return f"{self.date} {self.train.code} {self.train.start_time} {self.seat.name}({self.train.count(self.seat.code)})"


def build_seats(task: TaskConfig) -> List[Seat]:
    """按任务配置得到有序席别列表；allow_upgrade 时把更贵的席别追加在后面。"""
    seats: List[Seat] = []
    seen = set()
    for token in (task.seats or []):
        sd = seat_of(token)
        if sd and sd.code not in seen:
            seen.add(sd.code)
            seats.append(sd)
    if task.allow_upgrade:
        # 追加同车更高等级席别作为兜底（商务 > 一等 > 二等 > 无座）
        rank = ["9", "P", "M", "O", "6", "4", "3", "2", "1", "W"]
        base = min((rank.index(s.code) for s in seats if s.code in rank), default=len(rank) - 1)
        for code in rank[:base + 1]:
            if code not in seen:
                seen.add(code)
                seats.append(SEATS[code])
    if task.allow_no_seat and "W" not in seen:
        seats.append(SEATS["W"])
    return seats


def filter_trains(trains: Iterable[Train], task: TaskConfig) -> List[Train]:
    """按车次/时间窗/站点筛掉不合适的车。"""
    trains = list(trains)
    wanted = {x.strip().upper() for x in (task.trains or []) if x.strip()}
    banned = {x.strip().upper() for x in (task.exclude_trains or []) if x.strip()}
    out = []
    for t in trains:
        code = t.code.upper()
        if t.not_open:
            continue
        if wanted and code not in wanted:
            continue
        if code in banned:
            continue
        if task.depart_between and not _in_window(t.start_time, task.depart_between):
            continue
        if task.arrive_between and not _in_window(t.arrive_time, task.arrive_between):
            continue
        if task.exact_station and task._from_code and t.from_code != task._from_code:
            continue
        if task.exact_station and task._to_code and t.to_code != task._to_code:
            continue
        if not t.can_buy and not t.seats_available():
            continue
        out.append(t)
    # 时间排序，早的优先
    out.sort(key=lambda t: t.start_time)
    return out


def pick_candidates(trains: Iterable[Train], task: TaskConfig) -> List[Candidate]:
    """输出 (车次, 席别) 候选，按「席别优先级 → 发车时间」排序。"""
    seats = build_seats(task)
    res: List[Candidate] = []
    for t in trains:
        for sd in seats:
            n = t.count(sd.code)
            if n > 0:
                res.append(Candidate(t, sd, t.date))
                break   # 同一车次只取最高优先级席别，避免重复下单
    seat_rank = {s.code: i for i, s in enumerate(seats)}
    res.sort(key=lambda c: (seat_rank.get(c.seat.code, 99), c.train.start_time))
    return res


def resolve_station_codes(task: TaskConfig, strict: bool = True) -> Tuple[str, str]:
    """站名 → 电报码，并写回 task 供 exact_station 使用。"""
    fc = book.code(task.from_station)
    tc = book.code(task.to_station)
    if strict and (not fc or not tc):
        missing = task.from_station if not fc else task.to_station
        sug = book.suggest(missing, 6)
        hint = "、".join(f"{n}({c})" for n, c in sug) if sug else "无匹配"
        raise QueryError(f"站点无法识别: {missing}；候选: {hint}")
    task._from_code = fc
    task._to_code = tc
    return fc, tc


# ---------------------------------------------------------------- 区间展开
def expand_dates(task: TaskConfig) -> List[str]:
    """把 dates 展开成合法日期；超过预售期（15 天）的直接裁掉。"""
    today = dt.date.today()
    horizon = today + dt.timedelta(days=14)
    out = []
    for d in task.resolved_dates():
        try:
            dd = dt.date.fromisoformat(d)
        except Exception:
            continue
        if dd < today or dd > horizon:
            continue
        out.append(dd.isoformat())
    return out


# ---------------------------------------------------------------- 便捷
def quick_search(from_station: str, to_station: str, date: str,
                 seat: str = "二等座", session: Optional[Session] = None) -> List[Train]:
    """供 Web/CLI 用的单次查询。"""
    sess = session or Session("quick")
    try:
        fc, tc = book.code(from_station), book.code(to_station)
        if not fc or not tc:
            raise QueryError(f"站点无法识别: {from_station}/{to_station}")
        q = Query(sess)
        trains = q.fetch(date, fc, tc)
        sd = seat_of(seat)
        if sd:
            trains = [t for t in trains if t.count(sd.code) > 0]
        return trains
    finally:
        if session is None:
            sess.close()
