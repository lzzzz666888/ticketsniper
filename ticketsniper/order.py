# -*- coding: utf-8 -*-
"""下单链路。

与老项目最大差异：
1. 每一步都返回结构化结果 + 可恢复的错误分类，由 worker 决策重试还是换车；
2. REPEAT_SUBMIT_TOKEN 每一单重新抓（老项目会复用过期 token 导致 400）；
3. 排队查询走「自适应退避 + 超时上限」，不会无限卡死；
4. 支持选座（choose_seats）与多人同行；
5. 所有请求参数集中构造，缺字段不影响（12306 容忍多余参数，缺关键参数才失败）。
"""
from __future__ import annotations

import datetime as dt
import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .api.constants import (ERR_KEYWORDS, F, SEATS, URLS, Seat, seat_of)
from .api.station import book
from .config import Passenger, TaskConfig, config
from .log import log
from .net import Resp, Session, classify
from .query import Candidate, Train

Log = log("order")

# 12306 的 bed_level_order_num 是 30 个 0，多一个少一个都会被判参数错误
BED_LEVEL_NUM = "0" * 30

# 选座偏好 → 座位字母。二等座 A B C D F，一等座 A C D F
SEAT_LETTERS = {
    "9": ["A", "C", "F"],       # 商务座
    "P": ["A", "C", "F"],       # 特等座
    "M": ["A", "C", "D", "F"],  # 一等座
    "O": ["A", "B", "C", "D", "F"],  # 二等座
}

# 卧铺偏好：3 上/中/下 -> 用 1/2/3 表示下/中/上
BERTH_PREF = {"下铺": "1", "中铺": "2", "上铺": "3"}

# 座位字母语义：A/F 靠窗，C/D 过道（二等座 B 是中间座）
WINDOW_LETTERS = ("A", "F")
AISLE_LETTERS = ("C", "D")
MIDDLE_LETTERS = ("B",)

_PREF_WORDS = {
    "靠窗": WINDOW_LETTERS, "窗": WINDOW_LETTERS, "WINDOW": WINDOW_LETTERS,
    "W": WINDOW_LETTERS, "F": WINDOW_LETTERS, "A": WINDOW_LETTERS,
    "过道": AISLE_LETTERS, "走道": AISLE_LETTERS, "AISLE": AISLE_LETTERS,
    "C": AISLE_LETTERS, "D": AISLE_LETTERS,
    "中间": MIDDLE_LETTERS, "B": MIDDLE_LETTERS,
    "两人": ("A", "F"), "两连": ("D", "F"),
}


def resolve_choose_seats(task, seat_code: str, n_passengers: int) -> List[str]:
    """把任务里的座位偏好翻成 12306 的 choose_seats 参数。

    12306 只对动车组（二等/一等/商务/特等）开放选座，普速车传了也被忽略，
    所以这里对非动车席别直接返回空列表，避免提交垃圾参数。
    """
    if seat_code not in SEAT_LETTERS:
        return []
    prefs = [str(x).strip() for x in (getattr(task, "seat_prefer", None) or [])]
    if not prefs:
        return []
    valid = set(SEAT_LETTERS[seat_code])
    picked: List[str] = []
    for p in prefs:
        u = p.upper()
        if u in valid:                      # 直接写字母就精确用这个字母
            if u not in picked:
                picked.append(u)
            continue
        letters = _PREF_WORDS.get(u) or _PREF_WORDS.get(p)
        if not letters:
            continue
        for L in letters:
            if L in valid and L not in picked:
                picked.append(L)
    if not picked:
        return []
    # 每个乘客取一个，从偏好里按顺序分配；人多了就循环
    return [picked[i % len(picked)] for i in range(max(1, n_passengers))]


class OrderError(Exception):
    def __init__(self, msg: str, kind: str = "unknown", resp: Optional[Resp] = None):
        super().__init__(msg)
        self.kind = kind
        self.resp = resp


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    msg: str = ""
    kind: str = "ok"          # ok / sold_out / dup_order / auth / risk / timeout / queue / unknown
    train: str = ""
    date: str = ""
    seat: str = ""
    passengers: List[str] = field(default_factory=list)
    wait_seconds: float = 0.0
    raw: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "order_id": self.order_id, "msg": self.msg,
            "kind": self.kind, "train": self.train, "date": self.date,
            "seat": self.seat, "passengers": self.passengers,
            "wait_seconds": round(self.wait_seconds, 1),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


# ---------------------------------------------------------------- 参数拼装
def _id_type_code(p: Passenger) -> str:
    return str(p.id_type or "1")


def passenger_ticket_str(p: Passenger, seat_code: str, choose: str = "") -> str:
    """单人票串：席别,0,票种,姓名,证件类型,证件号,手机号,N[,座位]"""
    sd = SEATS.get(seat_code) or seat_of(seat_code)
    st = sd.order_code if sd else seat_code
    parts = [
        st, "0", str(p.ticket_type or "1"),
        p.name, _id_type_code(p), p.id_no, p.phone or "", "N",
    ]
    if choose:
        parts.append(choose)
    return ",".join(parts)


def old_passenger_str(p: Passenger) -> str:
    """老格式乘客串：姓名,证件类型,证件号,票种_"""
    return f"{p.name},{_id_type_code(p)},{p.id_no},{p.ticket_type or '1'}_"


def build_ticket_strings(passengers: Sequence[Passenger], seat_code: str,
                         choose_seats: Sequence[str] = ()) -> Tuple[str, str]:
    """多人出行：乘客串之间用 _ 连接，老串直接拼接。"""
    tickets, olds = [], []
    for idx, p in enumerate(passengers):
        ch = choose_seats[idx] if idx < len(choose_seats) else ""
        tickets.append(passenger_ticket_str(p, seat_code, ch))
        olds.append(old_passenger_str(p))
    return "_".join(tickets), "".join(olds)


def _date_for_queue(date: str) -> str:
    """getQueueCount 要求 'Sat Nov 07 2026 00:00:00 GMT+0800 (China Standard Time)' 这种格式。"""
    d = dt.date.fromisoformat(date)
    return d.strftime("%a %b %d %Y 00:00:00 GMT+0800 (China Standard Time)")


def _fmt_ts() -> str:
    return str(int(time.time() * 1000))


# ---------------------------------------------------------------- 下单器
class Orderer:
    """一个 session 对应一个 Orderer。线程安全靠 session 自身的锁 + 本类锁。"""

    def __init__(self, session: Session):
        self.s = session
        self._lock = threading.Lock()
        self.last_error = ""

    # ---------- 1. 提交订单请求（锁座位）
    def submit(self, cand: Candidate, task: TaskConfig) -> str:
        """成功返回 repeat_submit_token 以外的标志（此处直接返回 ''）。"""
        t = cand.train
        data = {
            "secretStr": urllib.parse.unquote(t.secret_str),
            "train_date": t.date,
            "back_train_date": dt.date.today().isoformat(),
            "tour_flag": "dc",
            "purpose_codes": "ADULT",
            "query_from_station_name": t.from_name or task.from_station,
            "query_to_station_name": t.to_name or task.to_station,
            "undefined": "",
        }
        r = self.s.post(URLS["submit_order"], data=data)
        jd = r.json_data or {}
        if not r.ok or not isinstance(jd, dict):
            raise OrderError(f"submitOrderRequest 失败: {r.error or r.text[:120]}", "http", r)
        if jd.get("status") is True:
            return ""
        msg = str(jd.get("messages") or jd.get("data") or r.text[:200])
        kind = self._kind_of(msg)
        raise OrderError(f"提交订单被拒: {msg}", kind, r)

    # ---------- 2. initDc 拿 token 与票信息
    def init_dc(self) -> Dict[str, str]:
        r = self.s.post(URLS["init_dc"], data={"_json_att": ""})
        html = r.text or ""
        if not r.ok or "globalRepeatSubmitToken" not in html:
            raise OrderError(f"initDc 失败: {r.status} {html[:120]}", "auth" if r.status in (302, 401) else "http", r)
        token = ""
        m = re.search(r"globalRepeatSubmitToken\s*=\s*'([^']+)'", html)
        if m:
            token = m.group(1)
        self.s.global_repeat_submit_token = token
        self.s.repeat_submit_token = token
        # ticketInfoForPassengerForm 是 JSON
        ti = ""
        m = re.search(r"ticketInfoForPassengerForm\s*=\s*(\{.*?\});", html, re.S)
        if m:
            ti = m.group(1)
        # leftTicketStr / purpose_codes 等也在 form 里
        return {"token": token, "form": ti, "html_len": str(len(html))}

    # ---------- 3. 乘客列表
    def passengers(self) -> List[dict]:
        r = self.s.post(URLS["passengers"], data={
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": self.s.repeat_submit_token or "",
        })
        jd = r.json_data or {}
        if isinstance(jd, dict) and jd.get("status") is True:
            rows = ((jd.get("data") or {}).get("normal_passengers")) or []
            self.s.passenger_dtos = rows
            return rows
        msg = str((jd or {}).get("messages") or r.text[:160])
        if "登录" in msg:
            raise OrderError(f"取乘客失败（需重新登录）: {msg}", "auth", r)
        raise OrderError(f"取乘客失败: {msg}", "unknown", r)

    # ---------- 4. 校验订单
    def check(self, pstr: str, ostr: str, token: str) -> dict:
        data = {
            "cancel_flag": "2",
            "bed_level_order_num": BED_LEVEL_NUM,
            "passengerTicketStr": pstr,
            "oldPassengerStr": ostr,
            "tour_flag": "dc",
            "randCode": "",
            "whatsSelect": "1",
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        r = self.s.post(URLS["check_order"], data=data)
        jd = r.json_data or {}
        if not isinstance(jd, dict):
            raise OrderError(f"checkOrderInfo 非 JSON: {r.text[:120]}", "http", r)
        if jd.get("status") is True:
            d = jd.get("data") or {}
            if d.get("submitStatus") is True:
                return d
            msg = str(d.get("errMsg") or jd.get("messages") or "校验未通过")
            raise OrderError(f"校验订单失败: {msg}", self._kind_of(msg), r)
        msg = str(jd.get("messages") or jd.get("data") or "校验失败")
        raise OrderError(f"checkOrderInfo: {msg}", self._kind_of(msg), r)

    # ---------- 5. 排队人数
    def queue_count(self, cand: Candidate, token: str) -> Dict[str, str]:
        t = cand.train
        data = {
            "train_date": _date_for_queue(t.date),
            "train_no": t.train_no,
            "stationTrainCode": t.code,
            "seatType": cand.seat.order_code,
            "fromStationTelecode": t.from_code,
            "toStationTelecode": t.to_code,
            "leftTicket": t.yp_info,
            "purpose_codes": "00",
            "train_location": t.location,
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        r = self.s.post(URLS["queue_count"], data=data)
        jd = r.json_data or {}
        if not isinstance(jd, dict) or jd.get("status") is not True:
            msg = str((jd or {}).get("messages") or r.text[:120])
            raise OrderError(f"getQueueCount: {msg}", self._kind_of(msg), r)
        d = jd.get("data") or {}
        return {k: str(v) for k, v in d.items()}

    # ---------- 6. 确认排队（真正锁票）
    def confirm(self, cand: Candidate, pstr: str, ostr: str, token: str,
                choose_seats: str = "") -> dict:
        t = cand.train
        data = {
            "passengerTicketStr": pstr,
            "oldPassengerStr": ostr,
            "randCode": "",
            "whatsSelect": "1",
            "seatDetailType": "000",
            "is_jy": "1",
            "is_cj": "0",
            "choose_seats": choose_seats,
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        r = self.s.post(URLS["confirm_queue"], data=data)
        jd = r.json_data or {}
        if not isinstance(jd, dict):
            raise OrderError(f"confirm 非 JSON: {r.text[:120]}", "http", r)
        if jd.get("status") is True:
            d = jd.get("data") or {}
            if str(d.get("submitStatus")).lower() == "true":
                return d
            msg = str(d.get("errMsg") or "确认失败")
            raise OrderError(f"确认排队失败: {msg}", self._kind_of(msg), r)
        msg = str(jd.get("messages") or "确认失败")
        raise OrderError(f"confirmSingleForQueue: {msg}", self._kind_of(msg), r)

    # ---------- 7. 轮询排队结果
    def wait_result(self, token: str, timeout: float = 90) -> Dict[str, str]:
        """返回 {'order_id':..., 'status':...}；超时抛 OrderError(kind='timeout')。"""
        t0 = time.time()
        delay = 1.0
        last: Dict[str, str] = {}
        while time.time() - t0 < timeout:
            params = {
                "randomEnd": _fmt_ts(),
                "tourFlag": "dc",
                "_json_att": "",
                "REPEAT_SUBMIT_TOKEN": token,
            }
            r = self.s.get(URLS["wait_time"] + "?" + urllib.parse.urlencode(params))
            jd = r.json_data or {}
            if isinstance(jd, dict) and jd.get("status") is True:
                d = jd.get("data") or {}
                last = {k: str(v) for k, v in d.items()}
                oid = str(d.get("orderId") or "").strip()
                qt = d.get("queryOrderWaitTimeStatus")
                if oid and oid not in ("0", "None", "null"):
                    return {"order_id": oid, "status": "ok", "raw": str(d)}
                wt = d.get("waitTime")
                if isinstance(wt, (int, float)) and wt > 0:
                    delay = min(max(float(wt) / 1000.0, 0.8), 6.0)
                msg = str(d.get("msg") or "")
                if msg:
                    self.last_error = msg
                if str(qt).lower() == "false" and not oid:
                    # 结束但没有订单号：可能失败
                    if "未处理" in msg or "取消" in msg:
                        raise OrderError(f"排队失败: {msg}", self._kind_of(msg), r)
            time.sleep(delay + random.uniform(0, 0.4))
            delay = min(delay * 1.25, 6.0)
        raise OrderError(f"排队超时（{timeout}s），订单可能在后台生成，请到 APP 查订单", "timeout")

    # ---------- 8. 确认出票到队列（老接口，部分场景需要）
    def result_order(self, order_id: str, token: str) -> bool:
        r = self.s.post(URLS["order_result"], data={
            "orderSequence_no": order_id,
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        })
        jd = r.json_data or {}
        return bool(isinstance(jd, dict) and jd.get("status") is True)

    # ---------- 组合流程
    def place(self, cand: Candidate, task: TaskConfig, passengers: Sequence[Passenger],
              choose_seats: Sequence[str] = (), wait_timeout: float = 90) -> OrderResult:
        t0 = time.time()
        t = cand.train
        base = dict(train=t.code, date=t.date, seat=cand.seat.name,
                    passengers=[p.name for p in passengers])
        with self._lock:
            try:
                if not self.s.is_login:
                    raise OrderError("未登录", "auth")
                pstr, ostr = build_ticket_strings(passengers, cand.seat.code, choose_seats)
                cs = ""
                if choose_seats:
                    cs = ",".join(f"{i + 1}{s}" for i, s in enumerate(choose_seats))

                self.submit(cand, task)
                Log.debug(f"{t.code} submitOrderRequest OK")

                info = self.init_dc()
                token = info["token"]
                if not token:
                    raise OrderError("initDc 未取到 REPEAT_SUBMIT_TOKEN", "unknown")

                self.check(pstr, ostr, token)
                Log.debug(f"{t.code} checkOrderInfo OK")

                q = self.queue_count(cand, token)
                Log.info(f"{t.code} {cand.seat.name} 排队: 前 {q.get('countT', '?')} 人 / 余票 {q.get('ticket', '?')}")

                # op_2 == 'true' 表示该席别无票
                if str(q.get("op_2")).lower() == "true":
                    raise OrderError(f"{t.code} {cand.seat.name} 余票不足", "sold_out")

                self.confirm(cand, pstr, ostr, token, cs)
                Log.ok(f"{t.code} {cand.seat.name} 已进入队列，等待分配座位")

                wr = self.wait_result(token, timeout=wait_timeout)
                oid = wr.get("order_id", "")
                self.result_order(oid, token)
                return OrderResult(
                    ok=True, order_id=oid, msg="下单成功",
                    train=t.code, date=t.date, seat=cand.seat.name,
                    passengers=[p.name for p in passengers],
                    wait_seconds=time.time() - t0,
                    raw={"queue": str(q), "wait": wr.get("raw", "")},
                )
            except OrderError as e:
                self.last_error = str(e)
                return OrderResult(
                    ok=False, msg=str(e), kind=e.kind,
                    wait_seconds=time.time() - t0, **base,
                )
            except Exception as e:   # 网络/解析异常
                self.last_error = f"{type(e).__name__}: {e}"
                Log.exc(f"{t.code} 下单异常")
                return OrderResult(ok=False, msg=self.last_error, kind="unknown",
                                   wait_seconds=time.time() - t0, **base)

    # ---------- 工具
    @staticmethod
    def _kind_of(msg: str) -> str:
        s = str(msg)
        for kw, kind in ERR_KEYWORDS.items():
            if kw in s:
                return kind
        return "unknown"

    # ---------- 已支付订单检查（避免重复下单被判 dup_order）
    def unfinished_orders(self) -> List[dict]:
        r = self.s.post(URLS["order_list"], data={
            "queryType": "1", "queryStartDate": "", "queryEndDate": "",
            "come_from_flag": "my_order", "pageSize": "8", "pageIndex": "0",
            "query_where": "G", "seuHcmf": "", "ursChecked": "false",
            "_json_att": "",
        })
        jd = r.json_data or {}
        if not isinstance(jd, dict):
            return []
        return ((jd.get("data") or {}).get("OrderDTODataList")) or []
