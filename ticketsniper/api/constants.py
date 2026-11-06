# -*- coding: utf-8 -*-
"""12306 协议常量层。

所有接口路径、字段下标、席别编码集中在此，升级时只改这一个文件。
字段下标来自 2026-11-07 线上实测（kyfw.12306.cn queryZ 返回 59 段）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

BASE = "https://kyfw.12306.cn"
HOST = "kyfw.12306.cn"
PASSPORT = BASE + "/passport"
OTN = BASE + "/otn"

# ---------------------------------------------------------------- 接口路径
URLS = {
    # 会话预热 / 设备指纹
    "init": OTN + "/leftTicket/init",
    "log_device": OTN + "/HttpZF/logdevice",
    "get_browser_device_id": OTN + "/HttpZF/getBrowserDeviceId",
    # 登录
    "login": PASSPORT + "/web/login",
    "login_check": OTN + "/login/conf",
    "uamtk": PASSPORT + "/web/auth/uamtk",
    "uamauthclient": OTN + "/uamauthclient",
    "logout": PASSPORT + "/web/logout",
    "qr_create": PASSPORT + "/web/create-qr64",
    "qr_check": PASSPORT + "/web/checkqr",
    "captcha_image": PASSPORT + "/captcha/captcha-image64",
    "captcha_check": PASSPORT + "/captcha/captcha-check",
    # 余票
    "query": OTN + "/leftTicket/{route}",
    # 乘车人
    "passengers": OTN + "/confirmPassenger/getPassengerDTOs",
    "user_info": OTN + "/modifyUser/initQueryUserInfoApi",
    "passenger_init": OTN + "/confirmPassenger/initDc",
    # 下单链路
    "submit_order": OTN + "/leftTicket/submitOrderRequest",
    "init_dc": OTN + "/confirmPassenger/initDc",
    "check_order": OTN + "/confirmPassenger/checkOrderInfo",
    "queue_count": OTN + "/confirmPassenger/getQueueCount",
    "confirm_queue": OTN + "/confirmPassenger/confirmSingleForQueue",
    "wait_time": OTN + "/confirmPassenger/queryOrderWaitTime",
    "order_result": OTN + "/confirmPassenger/resultOrderForDcQueue",
    "order_list": OTN + "/queryOrder/queryMyOrderNoComplete",
    # 候补（放票失败兜底）
    "after_submit": OTN + "/afterNate/submitOrder",
    "after_rate": OTN + "/afterNate/successRate",
    # 健康检查（CDN 存活探针，实测返回 200 即通）
    "cdn_probe": "https://{host}/otn/dynamicJs/omseuuq",
}

# 余票查询多路由：线上四个入口返回集合略有差异，竞速取先到的合法 JSON
QUERY_ROUTES = ("queryZ", "queryA", "queryG", "queryO")

# 单次查询必须带的固定参数
QUERY_PARAMS = {"purpose_codes": "ADULT"}


# ---------------------------------------------------------------- 席别
@dataclass(frozen=True)
class Seat:
    code: str          # 下单用的 seatType
    name: str          # 中文名
    field: int         # 余票字符串里的下标
    short: str         # 记日志用的缩写

    @property
    def order_code(self) -> str:
        # getQueueCount / confirmSingleForQueue 用的 seatType
        return "1" if self.code in ("1", "W") else self.code


SEATS: Dict[str, Seat] = {
    "9": Seat("9", "商务座", 32, "SWZ"),
    "P": Seat("P", "特等座", 25, "TZ"),
    "M": Seat("M", "一等座", 31, "ZY"),
    "O": Seat("O", "二等座", 30, "ZE"),
    "6": Seat("6", "高级软卧", 21, "GR"),
    "4": Seat("4", "软卧", 23, "RW"),
    "3": Seat("3", "硬卧", 28, "YW"),
    "2": Seat("2", "软座", 24, "RZ"),
    "1": Seat("1", "硬座", 29, "YZ"),
    "W": Seat("W", "无座", 26, "WZ"),
    "F": Seat("F", "动卧", 33, "DW"),
    "A": Seat("A", "高级动卧", 20, "GG"),
}

# 中文名 / 拼音缩写 → code，配置里允许三种写法
SEAT_ALIAS: Dict[str, str] = {}
for _c, _s in SEATS.items():
    SEAT_ALIAS[_s.name] = _c
    SEAT_ALIAS[_s.short] = _c
    SEAT_ALIAS[_c] = _c
SEAT_ALIAS.update({
    "二等": "O", "一等": "M", "商务": "9", "硬卧下铺": "3", "卧铺": "3",
    "软卧下铺": "4", "无坐": "W", "站票": "W", "ZE": "O", "ZY": "M",
})


def seat_of(token: str) -> Optional[Seat]:
    """把配置里的席别写法解析成 Seat。"""
    if token is None:
        return None
    t = str(token).strip()
    code = SEAT_ALIAS.get(t) or SEAT_ALIAS.get(t.upper()) or (t if t in SEATS else None)
    return SEATS.get(code) if code else None


# ---------------------------------------------------------------- 余票字段下标
# 实测 59 段；0..33 与官方 lcquery.js 一致，34+ 为扩展段
F = {
    "secret_str": 0, "button_text": 1, "train_no": 2, "train_code": 3,
    "start_station": 4, "end_station": 5, "from_station": 6, "to_station": 7,
    "start_time": 8, "arrive_time": 9, "duration": 10, "can_buy": 11,
    "yp_info": 12, "train_date": 13, "seat_feature": 14, "location": 15,
    "from_no": 16, "to_no": 17, "support_card": 18, "controlled": 19,
    "gg_num": 20, "gr_num": 21, "qt_num": 22, "rw_num": 23, "rz_num": 24,
    "tz_num": 25, "wz_num": 26, "yb_num": 27, "yw_num": 28, "yz_num": 29,
    "ze_num": 30, "zy_num": 31, "swz_num": 32, "srrb_num": 33,
    "yp_ex": 34, "seat_types": 35, "exchange": 36, "houbu_flag": 37,
    "houbu_limit": 38, "yp_detail": 39, "is_async": 45,
}

# 允许下单的状态（canWebBuy）
CAN_BUY = {"Y", "y"}
# 明确表示"不可买"的状态，用于日志区分"没票"和"停售"
NOT_BUY = {"N", "n", "IS_TIME_NOT_BUY", "1"}

# 可用席别数量解析：'有' / '无' / 数字 / '*'
NO_TICKET_TOKENS = {"无", "", "--", "*"}


def parse_count(raw: str) -> int:
    """把余票字段转成整数；'有' 用 9999 表示充足，'无' 表示 0。"""
    if raw is None:
        return 0
    t = str(raw).strip()
    if t in ("有", "充足"):
        return 9999
    if t in NO_TICKET_TOKENS:
        return 0
    try:
        return int(t)
    except ValueError:
        return 0


# ---------------------------------------------------------------- 错误码
# 12306 用 messages/errMsg 文本区分业务错误，这里做关键词归类
ERR_KEYWORDS = {
    "auth": ("登录", "未登录", "请重新登录", "user", "uamtk", "验证码"),
    "risk": ("网络可能存在问题", "访问过于频繁", "拒绝", "慢一点", "排队人数"),
    "sold_out": ("余票不足", "无票", "已售完", "票额不足", "没有足够的票"),
    "dup_order": ("未处理", "重复", "存在未完成订单", "已有订单"),
    "time_out": ("超时", "timeout"),
    "not_open": ("未开售", "起售", "不在预售期"),
    "queue_full": ("排队", "人数过多", "系统繁忙"),
}
