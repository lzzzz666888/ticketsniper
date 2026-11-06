# -*- coding: utf-8 -*-
"""离线链路自测：用本地 mock 覆盖 12306 下单全链路。

为什么要这个：真实下单必须有账号，开发期不能反复用真号试错。
本模块起一个本地 HTTP 服务，精确复刻各接口的返回体形状，
让 Orderer 把 submitOrderRequest → initDc → checkOrderInfo →
getQueueCount → confirmSingleForQueue → queryOrderWaitTime →
resultOrderForDcQueue 完整走一遍，断言参数拼装与状态机都对。

用法：
    python -m ticketsniper.selftest            # 跑全部
    python -m ticketsniper.selftest -v         # 打印每步请求参数
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERBOSE = False

# Windows 控制台默认 GBK，输出 ✓/✗ 会炸，这里强制 UTF-8
try:
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 记录所有收到的请求，供断言
RECORD: list = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------------- 工具
    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "ignore") if n else ""
        d = {}
        for kv in raw.split("&"):
            if "=" in kv:
                k, _, v = kv.partition("=")
                d[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
        return d

    # ---------------- 路由
    def do_GET(self):
        path, _, qs = self.path.partition("?")
        RECORD.append(("GET", path, dict(urllib.parse.parse_qsl(qs))))
        if path == "/otn/leftTicket/init":
            return self._send({"status": True, "httpstatus": 200})
        if path == "/otn/confirmPassenger/initDc":
            return self._send_html(_INIT_DC_HTML)
        if path == "/otn/confirmPassenger/queryOrderWaitTime":
            return self._send({
                "status": True, "httpstatus": 200,
                "data": {"queryOrderWaitTimeStatus": True, "count": 3,
                         "waitTime": 200, "requestId": 1,
                         "orderId": "E123456789", "msg": "排队成功"},
            })
        if path == "/otn/login/conf":
            return self._send({"status": True, "data": {"is_login": "Y",
                                                        "is_login_passCode": "N"}})
        return self._send({"status": True})

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        p = self._params()
        RECORD.append(("POST", path, p))
        if VERBOSE:
            print(f"  → POST {path} {p}")

        if path == "/otn/leftTicket/submitOrderRequest":
            assert p.get("secretStr"), "secretStr 不能为空"
            assert p.get("tour_flag") == "dc"
            assert p.get("purpose_codes") == "ADULT"
            return self._send({"status": True, "httpstatus": 200,
                               "data": "N", "messages": []})
        if path == "/otn/confirmPassenger/initDc":
            return self._send_html(_INIT_DC_HTML)
        if path == "/otn/confirmPassenger/getPassengerDTOs":
            return self._send({"status": True, "data": {"normal_passengers": [
                {"passenger_name": "张三", "passenger_id_no": "110101199001011234",
                 "passenger_id_type_code": "1", "passenger_type": "1",
                 "mobile_no": "13800000000"}]}})
        if path == "/otn/confirmPassenger/checkOrderInfo":
            pt = p.get("passengerTicketStr", "")
            assert pt.count(",") >= 7, f"passengerTicketStr 字段数不对: {pt}"
            assert p.get("bed_level_order_num") == "0" * 30
            assert p.get("cancel_flag") == "2"
            return self._send({"status": True,
                               "data": {"submitStatus": True, "errMsg": ""}})
        if path == "/otn/confirmPassenger/getQueueCount":
            assert "GMT+0800" in p.get("train_date", ""), "train_date 格式不对"
            assert p.get("purpose_codes") == "00"
            return self._send({"status": True, "data": {
                "countT": "12", "ticket": "25,0", "op_2": "false",
                "op_1": "false", "count": "12"}})
        if path == "/otn/confirmPassenger/confirmSingleForQueue":
            assert p.get("seatDetailType") == "000"
            assert p.get("submitToken") is None  # 用 REPEAT_SUBMIT_TOKEN
            return self._send({"status": True,
                               "data": {"submitStatus": True, "errMsg": ""}})
        if path == "/otn/confirmPassenger/resultOrderForDcQueue":
            return self._send({"status": True, "data": "success"})
        if path == "/passport/web/auth/uamtk":
            return self._send({"result_code": 0, "newapptk": "tk-demo"})
        if path == "/otn/uamauthclient":
            return self._send({"result_code": 0, "username": "demo"})
        return self._send({"status": True})


_INIT_DC_HTML = """<html><body>
<script>
var globalRepeatSubmitToken = 'tok-abcdef123456';
var ticketInfoForPassengerForm = {
 'leftTicketStr': 'yp-demo',
 'purpose_codes': '00',
 'train_location': 'P2',
 'queryLeftNewDTO': {'train_no': '240000G53106', 'station_train_code': 'G531'}
};
</script></body></html>"""


def start_server(port: int = 0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------- 断言器
class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, cond: bool, name: str, extra: str = ""):
        if cond:
            self.passed += 1
            print(f"  ✓ {name}")
        else:
            self.failed.append(name)
            print(f"  ✗ {name} {extra}")

    def report(self) -> bool:
        print()
        if self.failed:
            print(f"失败 {len(self.failed)} 项 / 通过 {self.passed} 项")
            for f in self.failed:
                print("   -", f)
            return False
        print(f"全部通过（{self.passed} 项）")
        return True


def run(verbose: bool = False) -> bool:
    global VERBOSE
    VERBOSE = verbose
    srv, port = start_server()
    base = f"http://127.0.0.1:{port}"
    c = Checker()

    # ---- 1. SM4 ----
    print("[1] SM4 与 12306 前端一致")
    from .crypto_sm4 import encrypt_ecb, password_field, self_test
    c.ok(self_test(), "国标向量 + 官方 JS 向量")
    c.ok(encrypt_ecb("1234", "1234567890123456") == "woPrxebr8Xvyo1qG8QxAUA==",
         "encrypt_ecb 与 SM4.js 输出一致")
    c.ok(password_field("abc").startswith("@"), "password 带 @ 前缀")

    # ---- 2. 票串拼装 ----
    print("[2] 下单票串拼装")
    from .config import Passenger
    from .order import build_ticket_strings, _date_for_queue
    p1 = Passenger(name="张三", id_no="110101199001011234", phone="13800000000")
    p2 = Passenger(name="李四", id_no="110101199002022345")
    tstr, ostr = build_ticket_strings([p1, p2], "O")
    parts = tstr.split("_")
    c.ok(len(parts) == 2, "多人用 _ 分隔")
    c.ok(parts[0].split(",")[0] == "O", "二等座 order_code = O")
    c.ok(parts[0].split(",")[1] == "0", "第二段固定 0")
    c.ok(parts[0].split(",")[3] == "张三", "姓名在第四段")
    c.ok(ostr == "张三,1,110101199001011234,1_李四,1,110101199002022345,1_",
         "oldPassengerStr 格式", ostr)
    c.ok("GMT+0800" in _date_for_queue("2026-11-09"), "getQueueCount 日期格式")

    # ---- 3. 查询解析 ----
    print("[3] 余票行解析")
    from .query import parse_line
    line = ("%E4%B8%89%E6%9D%A1%7C%E9%A2%84%E8%AE%A2%7C240000G53106%7CG531%7CVNP%7CAOH%7CVNP%7CAOH"
            "%7C06%3A08%7C12%3A04%7C05%3A56%7CY%7Cyp%7C20261109%7C%7CP2%7C01%7C13%7CY%7C%7C%7C%7C%7C"
            "%7C%7C%7C%7C%7C%7C%7C30%7C5%7C0%7C%7C90M0O0W0%7C9MOO")
    tr = parse_line(line, "2026-11-09")
    c.ok(tr is not None, "解析成功")
    if tr:
        c.ok(tr.code == "G531", "车次", tr.code)
        c.ok(tr.from_code == "VNP", "上车站电报码", tr.from_code)
        c.ok(tr.seat_types == "9MOO", "席别串", tr.seat_types)
        c.ok(tr.count("O") == 30, "二等座余票 30", str(tr.count("O")))

    # ---- 4. 全链路下单（mock） ----
    print("[4] 下单全链路（mock 12306）")
    from .net import Session
    from .order import Orderer
    from .api.constants import SEATS
    from .query import Candidate, Train
    from .config import TaskConfig

    sess = Session("selftest", use_cdn=False)
    sess.http.base_url = base  # 不用，直接改 URLS
    import ticketsniper.api.constants as C
    import ticketsniper.order as O
    # 把 URLS 指向 mock
    old_urls = dict(C.URLS)
    for k, v in list(C.URLS.items()):
        C.URLS[k] = v.replace("https://kyfw.12306.cn", base)
    import ticketsniper.net as N
    N.URLS = C.URLS

    sess.is_login = True
    train = Train(
        secret_str="%E4%B8%89%E6%9D%A1", train_no="240000G53106", code="G531",
        from_code="VNP", to_code="AOH", from_name="北京南", to_name="上海虹桥",
        start_time="06:08", arrive_time="12:04", duration="05:56", can_buy="Y",
        date="2026-11-09", train_date_raw="20261109", location="P2",
        yp_info="yp-demo", seat_types="9MOO", from_no="01", to_no="13",
        yp_detail="90M0O0W0", raw=[], counts={"O": 30}, query_at=time.time(),
    )
    cand = Candidate(train=train, seat=SEATS["O"], date="2026-11-09")
    task = TaskConfig(id="T", name="test", from_station="北京南", to_station="上海虹桥")
    res = Orderer(sess).place(cand, task, [p1], wait_timeout=10)
    c.ok(res.ok, "place() 返回成功", res.msg)
    c.ok(res.order_id == "E123456789", "拿到 orderId", res.order_id)

    paths = [p for _, p, _ in RECORD]
    for need in ("/otn/leftTicket/submitOrderRequest",
                 "/otn/confirmPassenger/initDc",
                 "/otn/confirmPassenger/checkOrderInfo",
                 "/otn/confirmPassenger/getQueueCount",
                 "/otn/confirmPassenger/confirmSingleForQueue",
                 "/otn/confirmPassenger/queryOrderWaitTime"):
        c.ok(need in paths, f"调用 {need.rsplit('/', 1)[-1]}")

    sub = [p for m, path, p in RECORD if path.endswith("submitOrderRequest")]
    if sub:
        c.ok(sub[0].get("secretStr") == "三条", "secretStr 已 URL 解码",
             str(sub[0].get("secretStr"))[:30])

    # 还原 URLS，避免污染其它进程内调用
    C.URLS.clear()
    C.URLS.update(old_urls)

    # ---- 5. 路由切换（c_url） ----
    print("[5] 余票路由动态切换")
    from .net import Resp, Session as S2
    r = Resp(302, "", {"c_url": "leftTicket/queryG", "status": False}, {}, "u", 0.1, "", "direct")
    c.ok(isinstance(r.json_data, dict) and r.json_data.get("c_url") == "leftTicket/queryG",
         "302 + c_url 被识别")

    # ---- 6. 起售时间与放票窗口 ----
    print("[6] 起售时间 / 放票窗口调度")
    from .sale import ArmPlan
    plan = ArmPlan(open_at=1000.0, warmup=120, burst_before=3, burst_after=60)
    c.ok(plan.phase(500.0) == "idle", "距放票 > warmup → idle")
    c.ok(plan.phase(950.0) == "warmup", "距放票 50s → warmup")
    c.ok(plan.phase(999.0) == "burst", "距放票 1s → burst")
    c.ok(plan.phase(1030.0) == "burst", "放票后 30s 仍 burst")
    c.ok(plan.phase(1200.0) == "done", "放票后 200s → done")
    from .sale import SaleTimeBook
    bk = SaleTimeBook()
    bk._map = {"VNP": "12:45", "AOH": "13:45"}
    bk._name = {"VNP": "北京南"}
    c.ok(bk.of("VNP") == "12:45", "起售时刻查询")
    c.ok(bk.minutes_of("AOH") == 13 * 60 + 45, "HH:MM → 分钟")
    import datetime as _dt
    od = bk.open_datetime("VNP", "2026-11-30")
    c.ok(od is not None and od.date() == _dt.date(2026, 11, 16)
         and od.hour == 12 and od.minute == 45,
         "乘车日 11-30 → 放票 11-16 12:45", str(od))
    soon = bk.open_soon("VNP", "2026-11-30", within=86400 * 40,
                        now=_dt.datetime(2026, 11, 16, 12, 40))
    c.ok(soon is not None and 290 < soon < 310, "开抢倒计时", str(soon))

    # ---- 7. 选座偏好 ----
    print("[7] 选座偏好解析")
    from .config import TaskConfig
    from .order import resolve_choose_seats
    t = TaskConfig(seat_prefer=["靠窗"])
    c.ok(resolve_choose_seats(t, "O", 2) == ["A", "F"], "靠窗 → A/F")
    c.ok(resolve_choose_seats(TaskConfig(seat_prefer=["F"]), "O", 1) == ["F"],
         "字母精确匹配不被展开")
    c.ok(resolve_choose_seats(TaskConfig(seat_prefer=["过道"]), "M", 2) == ["C", "D"],
         "过道 → C/D")
    c.ok(resolve_choose_seats(TaskConfig(seat_prefer=["下铺"]), "3", 1) == [],
         "普速硬卧不支持选座，返回空")
    c.ok(resolve_choose_seats(TaskConfig(seat_prefer=[]), "O", 1) == [],
         "无偏好返回空")
    pstr, _ = build_ticket_strings([Passenger(name="张三", id_no="110101199001011234")],
                                   "O", ["F"])
    c.ok(pstr.endswith(",F"), "座位字母进乘客串第 8 段", pstr)

    # 8. 体检报告（不联网：只测纯逻辑的 Report 聚合与三种结论）
    print("\n[8] 体检报告聚合")
    from .doctor import Report
    r0 = Report()
    r0.ok("配置", "x")
    c.ok(r0.verdict() == "ok", "全 ok → verdict ok")
    r1 = Report()
    r1.ok("配置", "x")
    r1.warn("登录", "y")
    c.ok(r1.verdict() == "warn", "有 warn → verdict warn")
    r2 = Report()
    r2.err("网络", "连不上")
    c.ok(r2.verdict() == "fail", "有 err → verdict fail")
    c.ok(r2.counts()["err"] == 1 and r2.counts()["ok"] == 0, "counts 统计正确")
    txt = r2.render()
    c.ok("❌" in txt and "结论" in txt, "render 含图标与结论", txt.splitlines()[-1])

    # 9. 乘客解析与通知文案（回归：乘车人匹配失败不能抹掉任务配置）
    print("\n[9] 乘客解析与通知")
    from .config import TaskConfig as TC
    from .notify import _pax_text
    from .worker import TaskWorker

    class _R:
        stop_event = threading.Event()
        stopped = False

    task = TC(id="TX", passengers=[Passenger(name="张三")])
    w = TaskWorker(task, _R(), dry=True)
    w.orderer.passengers = lambda: [{"passenger_name": "张三",
                                     "passenger_id_no": "110101199001011234",
                                     "passenger_id_type_code": "1",
                                     "passenger_type": "1", "mobile_no": ""}]
    got = w._resolve_passengers()
    c.ok(len(got) == 1 and got[0].id_no == "110101199001011234", "按姓名同步到证件号")
    w.orderer.passengers = lambda: []
    w2 = TaskWorker(TC(id="TY", passengers=[Passenger(name="李四")]), _R(), dry=True)
    w2.orderer.passengers = lambda: []
    c.ok(w2._resolve_passengers() == [], "账号下没这个人时返回空")
    c.ok([p.name for p in w2.task.passengers] == ["李四"],
         "匹配失败不抹掉任务配置（回归）")
    c.ok(_pax_text("张三, 李四") == "张三、李四", "字符串乘客名不按字符拆")
    c.ok(_pax_text([Passenger(name="张三"), Passenger(name="李四")]) == "张三、李四",
         "列乘客名单正常拼接")

    srv.shutdown()
    return c.report()


def main(argv=None):
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    ok = run(verbose="-v" in argv or "--verbose" in argv)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
