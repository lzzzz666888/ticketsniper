# -*- coding: utf-8 -*-
"""命令行入口。

  python -m ticketsniper run                 # 按 tasks.yaml 开始抢
  python -m ticketsniper run -t T01,T02      # 指定任务
  python -m ticketsniper query 北京南 上海虹桥 2026-11-09 --seat 二等座
  python -m ticketsniper login               # 扫码登录并存 cookie
  python -m ticketsniper cdn                 # 探测 CDN 节点
  python -m ticketsniper web                 # 起控制台
  python -m ticketsniper init                # 生成默认配置
  python -m ticketsniper status              # 看订单/任务
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .config import (Config, bootstrap, config, ensure_dirs, paths, store)
from .log import log, recent, setup as setup_log

Log = log("cli")

BANNER = r"""
  ______ _ _        _        _____       _
 |__   _(_) | ___  | |      / ____|     (_)
    | | | | |/ _ \ | |     | (___  _ __  _ _ __   ___ _ __
    | | | | |  __/ | |      \___ \| '_ \| | '_ \ / _ \ '__|
    | | |_| |_|\___| |_     ____) | | | | | |_) |  __/ |
    |_|  |_|\___/|_|_|    |_____/|_| |_|_| .__/ \___|_|
                                          | |
       12306 抢票引擎 v1.0                 |_|
"""


def cmd_init(args):
    bootstrap()
    print(f"已生成:\n  {paths.CONFIG}\n  {paths.TASKS}")
    print("编辑 tasks.yaml 填乘客与车次，然后 python -m ticketsniper run")


def cmd_query(args):
    from .api.station import book
    from .net import Session
    from .query import Query, QueryError, build_seats
    from .api.constants import seat_of

    ensure_dirs()
    s = Session("cli")
    try:
        fc, tc = book.code(args.frm), book.code(args.to)
        if not fc or not tc:
            sug = book.suggest(args.frm or args.to, 8)
            print(f"站点无法识别。候选: {[f'{n}({c})' for n, c in sug]}")
            return 2
        q = Query(s)
        t0 = time.time()
        trains = q.fetch(args.date, fc, tc)
        el = time.time() - t0
        sd = seat_of(args.seat) if args.seat else None
        if sd:
            trains = [t for t in trains if t.count(sd.code) > 0]
        if args.depart:
            from .query import _in_window
            trains = [t for t in trains if _in_window(t.start_time, args.depart)]
        print(f"\n{args.frm}({fc}) → {args.to}({tc})  {args.date}  "
              f"{len(trains)} 趟  {el * 1000:.0f}ms\n")
        print(f"{'车次':<8}{'出发':<7}{'到达':<7}{'历时':<8}{'状态':<6}余票")
        print("-" * 78)
        for t in trains[: args.limit]:
            seats = " ".join(
                f"{k}:{'有' if v > 9000 else v}" for k, v in t.seats_available().items()
            )
            print(f"{t.code:<8}{t.start_time:<7}{t.arrive_time:<7}{t.duration:<8}"
                  f"{t.can_buy:<6}{seats}")
        return 0
    except QueryError as e:
        print(f"查询失败: {e}")
        return 1
    finally:
        s.close()


def cmd_login(args):
    from .login import LoginManager, qr_ascii
    from .net import Session

    ensure_dirs()
    s = Session("cli-login")
    mgr = LoginManager(s)
    cfg = config()
    try:
        mgr.prepare()
        if mgr.check_alive():
            print("cookie 仍然有效，无需登录")
            return 0
        if args.mode == "cookie":
            if not args.cookie:
                print("需要 --cookie 'JSESSIONID=...; ...'")
                return 2
            ok = mgr.login_by_cookie(args.cookie)
        elif args.mode == "password":
            if args.user:
                cfg.login.username = args.user
            if args.password:
                cfg.login.password = args.password
            if not cfg.login.username or not cfg.login.password:
                print("需要 --user 与 --password（或用环境变量 TS_PASSWORD）")
                return 2
            ok = mgr.login_by_password(_ask_captcha)
        else:
            ok = mgr.login_by_qr(on_qr=lambda p: print(qr_ascii(p)))
        if ok:
            mgr.activate()
            s.save_cookies()
            print("\n登录成功，cookie 已保存到", paths.COOKIE_JAR)
            if s.repeat_submit_token:
                print("订单令牌:", s.repeat_submit_token[:12], " 乘客:", len(s.passenger_dtos))
            return 0
        print("登录失败")
        return 1
    finally:
        s.close()


def _ask_captcha(img_path: str) -> str:
    print(f"\n验证码图片: {img_path}")
    try:
        os.startfile(img_path)   # Windows 直接打开
    except Exception:
        pass
    return input("输入验证码坐标（如 40,60,120,60）: ").strip()


def cmd_cdn(args):
    from .net import cdn_pool

    ensure_dirs()
    print("加载候选 IP…")
    nodes = cdn_pool.load()
    print(f"候选 {len(nodes)} 个，开始探测（并发 12）…")
    ok = cdn_pool.refresh(workers=12, limit=args.limit, progress=lambda i, n: None)
    cdn_pool.save()
    print(f"\n可用 {len(ok)} 个：")
    for n in sorted(ok, key=lambda x: x.score)[:30]:
        print(f"  {n.ip:<18} {n.rtt * 1000:7.1f} ms")
    return 0


def cmd_sale(args):
    """查起售时刻 / 放票倒计时。抢票的胜负手全在这里。"""
    import datetime as dt

    from .api.station import book
    from .sale import PRESALE_DAYS, SaleTimeBook

    ensure_dirs()
    code = book.code(args.station)
    if not code:
        print(f"站名无法识别: {args.station}  候选 {book.suggest(args.station, 8)}")
        return 2
    bk = SaleTimeBook().load()
    st = bk.of(code)
    if not st:
        print(f"{bk.name_of(code) or code}({code}) 不在起售时间表里")
        return 1
    nxt = bk.next_open(code)
    left = int((nxt - dt.datetime.now()).total_seconds()) if nxt else 0
    print(f"\n{bk.name_of(code) or args.station}（{code}）")
    print(f"  每日起售时刻  {st}   预售 {PRESALE_DAYS} 天（含当天）")
    print(f"  下次放票      {nxt:%Y-%m-%d %H:%M}  还有 {left // 3600}h{left % 3600 // 60}m")
    if args.date:
        od = bk.open_datetime(code, args.date)
        if od:
            delta = int((od - dt.datetime.now()).total_seconds())
            flag = "已放票" if delta < 0 else f"还有 {delta // 3600}h{delta % 3600 // 60}m"
            print(f"  {args.date} 的票  {od:%Y-%m-%d %H:%M} 放出（{flag}）")
    if args.table:
        rows = sorted(bk._map.items(), key=lambda kv: kv[1])
        print("\n  全站起售时刻分布：")
        from collections import Counter
        for t, n in Counter(v for _, v in rows).most_common():
            print(f"    {t}  {n:>4} 站")
    return 0


def cmd_passengers(args):
    """列出账号下的乘车人，方便填 tasks.yaml。"""
    from .login import LoginManager
    from .net import Session

    ensure_dirs()
    s = Session("cli")
    try:
        mgr = LoginManager(s)
        mgr.prepare()
        if not mgr.check_alive():
            if not mgr.ensure():
                print("未登录，先跑 python -m ticketsniper login")
                return 2
        mgr.activate()
        rows = []
        try:
            from .order import Orderer
            rows = Orderer(s).passengers()
        except Exception as e:
            print(f"取乘车人失败: {e}")
            return 1
        if not rows:
            print("账号下没有乘车人")
            return 0
        print(f"\n{'姓名':<10}{'证件类型':<8}{'证件号':<22}{'票种':<6}手机")
        print("-" * 70)
        for r in rows:
            name = r.get("passenger_name", "")
            it = r.get("passenger_id_type_name", "")
            idn = r.get("passenger_id_no", "")
            tt = r.get("passenger_type_name", "")
            ph = r.get("mobile_no", "")
            print(f"{name:<10}{it:<8}{idn:<22}{tt:<6}{ph}")
        print(f"\n共 {len(rows)} 人。把姓名填进 tasks.yaml 的 passengers 即可。")
        return 0
    finally:
        s.close()


def cmd_run(args):
    from .login import LoginManager, qr_ascii
    from .net import Session
    from .worker import Runner

    ensure_dirs()
    cfg = config()
    if args.rate:
        cfg.net.rate_per_sec = args.rate
    if args.interval:
        cfg.net.query_interval = args.interval
    if args.workers:
        cfg.net.query_workers = args.workers
    if args.headless_qr:
        cfg.login.mode = "qrcode"

    if not os.path.exists(paths.TASKS):
        bootstrap()
        print("已生成 tasks.yaml，请先填乘客与车次")
        return 2

    runner = Runner(cfg)
    runner.max_rounds = 1 if args.once else 0
    runner.on_success_cb = lambda t, r, c: print(f"\n✅ 抢到: {c.train.code} {c.seat.name} {r.order_id}\n")
    ids = [x.strip() for x in (args.tasks or "").split(",") if x.strip()] or None

    print(BANNER)
    n = runner.start(ids, dry=args.dry)
    if not n:
        return 1
    try:
        while runner.running():
            time.sleep(1)
            # 简要状态行
            if int(time.time()) % 10 == 0:
                snap = runner.snapshot()
                line = " | ".join(
                    f"{t['id']}:{t['state']}({t['queries']}q/{t['hits']}h)" for t in snap["tasks"]
                )
                sys.stdout.write(f"\r{line[:160]:<160}")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n收到中断，停止中…")
    finally:
        runner.stop()
    return 0


def cmd_web(args):
    from .web import serve

    ensure_dirs()
    bootstrap()
    serve(args.host, args.port)
    return 0


def cmd_status(args):
    from .store import list_orders, stats

    ensure_dirs()
    store.load()
    print(f"任务 {len(store.tasks)} 个:")
    for t in store.tasks:
        print(f"  {t.id:<6}{t.name:<28}{'启用' if t.enabled else '停用'}  "
              f"{t.from_station}→{t.to_station} {','.join(t.dates)}")
    s = stats()
    print(f"\n订单 总{s['total']} 成功{s['success']} 近24h{s['today']} 成功率{s['rate']}%")
    for o in list_orders(10):
        flag = "✅" if o["ok"] else "❌"
        print(f"  {flag} {o['at']} {o['train']} {o['date']} {o['seat']} "
              f"{o['passengers']} {o['msg'][:40]}")
    return 0


def cmd_doctor(args):
    from .doctor import main as doctor_main

    ids = [x.strip() for x in (args.tasks or "").split(",") if x.strip()] or None
    return doctor_main(ids)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("ticketsniper", description="12306 抢票引擎")
    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("init", help="生成默认配置")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("run", help="开始抢票")
    sp.add_argument("-t", "--tasks", help="任务 id，逗号分隔")
    sp.add_argument("--rate", type=float, help="全局 QPS")
    sp.add_argument("--interval", type=float, help="查询间隔秒")
    sp.add_argument("--workers", type=int, help="查询 worker 数")
    sp.add_argument("--once", action="store_true", help="一轮后退出")
    sp.add_argument("--dry", action="store_true", help="演练：只查询不下单（无需登录）")
    sp.add_argument("--headless-qr", action="store_true", help="强制扫码登录")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("query", help="查询余票")
    sp.add_argument("frm", nargs="?", default="北京南")
    sp.add_argument("to", nargs="?", default="上海虹桥")
    sp.add_argument("date", nargs="?", default=(lambda: (__import__("datetime").date.today()
                                                          + __import__("datetime").timedelta(days=2)).isoformat())())
    sp.add_argument("--seat", default="", help="只看某席别有票")
    sp.add_argument("--depart", default="", help="出发时间窗 HH:MM-HH:MM")
    sp.add_argument("--limit", type=int, default=40)
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("login", help="登录")
    sp.add_argument("--mode", default="qrcode", choices=["qrcode", "password", "cookie"])
    sp.add_argument("--user", default="")
    sp.add_argument("--password", default="")
    sp.add_argument("--cookie", default="")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("cdn", help="探测 CDN 节点")
    sp.add_argument("--limit", type=int, default=80)
    sp.set_defaults(func=cmd_cdn)

    sp = sub.add_parser("web", help="启动控制台")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("sale", help="查起售时刻/放票倒计时")
    sp.add_argument("station", nargs="?", default="北京南")
    sp.add_argument("--date", default="", help="查某天乘车日的票何时放出")
    sp.add_argument("--table", action="store_true", help="打印全站起售时刻分布")
    sp.set_defaults(func=cmd_sale)

    sp = sub.add_parser("passengers", help="列出账号下乘车人")
    sp.set_defaults(func=cmd_passengers)

    sp = sub.add_parser("doctor", help="开抢前体检（网络/路由/起售/登录/通知）")
    sp.add_argument("-t", "--tasks", default="", help="只查指定任务，逗号分隔")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("status", help="任务与订单")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    # Windows 控制台默认 GBK，直接 print 中文和 ✓ 会抛 UnicodeEncodeError / 乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    setup_log("DEBUG" if args.verbose else config().log_level)
    if not getattr(args, "func", None):
        print(BANNER)
        ap.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
