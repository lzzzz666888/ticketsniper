# -*- coding: utf-8 -*-
"""体检：一条命令看清「现在开抢会不会翻车」。

抢票是个依赖外部系统的长流程，任何一环不通都会等到放票那一刻才暴露。
本模块把启动前该验证的环节串起来一次跑完，输出带 ✅/⚠️/❌ 的报告：

  1. 配置与任务   —— 必填项、站点能否解析、日期是否在预售期内
  2. 网络          —— 能否直达 kyfw.12306.cn、延迟、是否需要代理
  3. 余票路由      —— 四个 query 路由哪个还活着（12306 会动态迁移）
  4. 起售时间表    —— 缓存是否新鲜、目标站起售时刻、距放票多久
  5. 登录态        —— cookie 是否还有效、账号下有几个乘车人
  6. CDN 池        —— 有几个可用节点
  7. 通知渠道      —— 配了哪些、能否发出

用法：
    python -m ticketsniper doctor
    python -m ticketsniper doctor -t T01     # 只检查某个任务
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from .api.constants import QUERY_ROUTES
from .api.station import book
from .config import TaskConfig, config, ensure_dirs, store
from .log import log
from .net import Session, cdn_pool
from .query import expand_dates, resolve_station_codes
from .sale import PRESALE_DAYS, SaleTimeBook

Log = log("doctor")


class Report:
    def __init__(self):
        self.items: List[Dict] = []

    def add(self, level: str, section: str, msg: str, hint: str = ""):
        self.items.append({"level": level, "section": section, "msg": msg, "hint": hint})

    # ---------------- 便捷写法
    def ok(self, section, msg, hint=""):
        self.add("ok", section, msg, hint)

    def warn(self, section, msg, hint=""):
        self.add("warn", section, msg, hint)

    def err(self, section, msg, hint=""):
        self.add("err", section, msg, hint)

    def info(self, section, msg, hint=""):
        self.add("info", section, msg, hint)

    # ---------------- 汇总
    def counts(self) -> Dict[str, int]:
        c = {"ok": 0, "warn": 0, "err": 0, "info": 0}
        for it in self.items:
            c[it["level"]] = c.get(it["level"], 0) + 1
        return c

    def verdict(self) -> str:
        c = self.counts()
        if c["err"]:
            return "fail"
        if c["warn"]:
            return "warn"
        return "ok"

    def to_dict(self) -> Dict:
        return {"verdict": self.verdict(), "counts": self.counts(), "items": self.items}

    def render(self) -> str:
        icon = {"ok": "✅", "warn": "⚠️ ", "err": "❌", "info": "· "}
        out, last = [], None
        for it in self.items:
            if it["section"] != last:
                out.append(f"\n[{it['section']}]")
                last = it["section"]
            line = f"  {icon.get(it['level'], '·')} {it['msg']}"
            if it["hint"]:
                line += f"\n      → {it['hint']}"
            out.append(line)
        c = self.counts()
        head = {"ok": "体检通过，可以开抢",
                "warn": "基本可用，但有几项要注意",
                "fail": "有硬伤，先修好再抢（否则放到票也抢不到）"}[self.verdict()]
        out.append(f"\n结论：{head}（✅{c['ok']} ⚠️{c['warn']} ❌{c['err']}）")
        return "\n".join(out).lstrip("\n")


# ------------------------------------------------------------------ 各项检查
def check_config(rep: Report, task_ids: Optional[List[str]] = None) -> List[TaskConfig]:
    ensure_dirs()
    store.load()
    tasks = [t for t in store.tasks if t.enabled and (not task_ids or t.id in task_ids)]
    if not store.tasks:
        rep.err("配置", "tasks.yaml 里没有任务", "先运行 ticketsniper init")
        return []
    if not tasks:
        rep.warn("配置", "没有启用的任务", "检查 tasks.yaml 的 enabled 字段")
        return []
    rep.ok("配置", f"已启用 {len(tasks)} 个任务")
    for t in tasks:
        try:
            fc, tc = resolve_station_codes(t, strict=False)
        except Exception as e:
            rep.err("配置", f"{t.id} 站点解析失败: {e}")
            continue
        if not fc or not tc:
            rep.err("配置", f"{t.id} 站点无效：{t.from_station} → {t.to_station}",
                    "用官方站名（如 北京南、上海虹桥）")
            continue
        dates = expand_dates(t)
        if not dates:
            rep.err("配置", f"{t.id} 日期全部落在预售期外（{PRESALE_DAYS} 天）",
                    f"把 dates 改成 '+1' 这类相对日期")
            continue
        if not t.seats:
            rep.warn("配置", f"{t.id} 没配席别，将使用默认二等座")
        pax = [p.name for p in t.passengers if p.name]
        if not pax:
            rep.err("配置", f"{t.id} 没配乘客姓名", "登录后用 `ticketsniper passengers` 查账号下的名字")
        rep.ok("配置", f"{t.id} {book.name(fc)}({fc}) → {book.name(tc)}({tc})  "
                     f"日期 {','.join(dates)}  席别 {'/'.join(t.seats)}  "
                     f"乘客 {'/'.join(pax) or '无'}")
    return tasks


def check_network(rep: Report, sess: Session):
    t0 = time.time()
    try:
        sess.preface()
        ms = (time.time() - t0) * 1000
        if ms > 3000:
            rep.warn("网络", f"直连 12306 成功但偏慢（{ms:.0f} ms）", "考虑配置代理或开 CDN")
        else:
            rep.ok("网络", f"直连 kyfw.12306.cn 正常（{ms:.0f} ms）")
    except Exception as e:
        rep.err("网络", f"连不上 12306: {e}", "检查网络/代理；企业网常被拦")


def check_routes(rep: Report, sess: Session, tasks: List[TaskConfig]):
    from .query import Query
    q = Query(sess)
    t = tasks[0] if tasks else None
    if t is None:
        return
    try:
        fc, tc = resolve_station_codes(t, strict=False)
        dates = expand_dates(t)
        if not fc or not tc or not dates:
            return
        trains = q.fetch(dates[0], fc, tc)
        ok_route = q.routes[0] if q.routes else QUERY_ROUTES[0]
        rep.ok("余票", f"查询正常：{dates[0]} 共 {len(trains)} 趟（当前路由 {ok_route}）")
        live = sum(1 for x in trains if x.can_buy)
        if not trains:
            rep.warn("余票", "这一天没查到车次（可能票未放或线路无直达）")
        elif live == 0:
            rep.warn("余票", f"{len(trains)} 趟车全部不可预订（未到起售时间或已售完）")
        else:
            rep.ok("余票", f"其中 {live} 趟可预订")
    except Exception as e:
        rep.err("余票", f"查询失败: {e}", "可能是路由迁移或网络抖动，重试一次")


def check_sale(rep: Report, sess: Session, tasks: List[TaskConfig]):
    st = SaleTimeBook(sess)
    try:
        st.load()
        n = len(st)
        rep.ok("起售", f"起售时间表已加载（{n} 个站）")
    except Exception as e:
        rep.err("起售", f"起售时间表拉取失败: {e}", "无法做放票窗口调度，会退化为匀速轮询")
        return
    for t in tasks:
        try:
            fc, _ = resolve_station_codes(t, strict=False)
            if not fc:
                continue
            hm = st.of(fc)
            if not hm:
                rep.warn("起售", f"{t.id} 查不到 {book.name(fc)} 的起售时刻",
                         "该站可能不在起售时间表里，将匀速轮询")
                continue
            secs = None
            for d in expand_dates(t):
                s = st.open_soon(fc, d, within=7 * 86400)
                if s is not None:
                    secs = s
                    break
            if secs is None:
                rep.info("起售", f"{t.id} {book.name(fc)} 每日 {hm} 放票；目标日期的票已放出或太远")
            elif secs > 0:
                rep.ok("起售", f"{t.id} {book.name(fc)} 每日 {hm} 放票，"
                               f"距下一个放票点 {int(secs // 3600)}h{int(secs % 3600 // 60)}m")
            else:
                rep.warn("起售", f"{t.id} 放票点刚过 {int(-secs)} 秒，正在窗口内",
                         "这是最佳抢票时机，直接开跑")
        except Exception as e:
            rep.warn("起售", f"{t.id} 起售检查异常: {e}")


def check_login(rep: Report, sess: Session):
    from .login import LoginManager
    mgr = LoginManager(sess)
    try:
        alive = mgr.check_alive()
    except Exception as e:
        rep.warn("登录", f"登录态检查失败: {e}")
        alive = False
    if alive:
        rep.ok("登录", f"cookie 有效（账号 {sess.username or '未知'}）")
        try:
            pax = mgr.refresh_order_context()
            n = len(sess.passenger_dtos or [])
            if n:
                rep.ok("登录", f"账号下有 {n} 个乘车人")
            else:
                rep.warn("登录", "账号下没有乘车人", "先在 12306 APP 里添加乘车人")
        except Exception as e:
            rep.warn("登录", f"乘客列表拉取失败: {e}")
    else:
        rep.warn("登录", "当前没有有效登录态",
                 "运行 `ticketsniper login` 扫码，或用 Web 控制台生成二维码")


def check_cdn(rep: Report):
    cfg = config().net
    if not cfg.use_cdn:
        rep.info("CDN", "未启用（config.yaml → net.use_cdn）")
        return
    try:
        nodes = cdn_pool.load()
        if not nodes:
            rep.warn("CDN", "CDN 池为空", "运行 `ticketsniper cdn` 探测一次")
        else:
            best = min(nodes, key=lambda x: x.score)
            rep.ok("CDN", f"{len(nodes)} 个节点可用，最优 {best.ip}（{best.rtt:.0f} ms）",
                   "CDN 只用于探测与 5xx 降级；余票/下单走直连（edge 会对查询返回 HTML）")
    except Exception as e:
        rep.warn("CDN", f"CDN 池加载失败: {e}")


def check_notify(rep: Report):
    n = config().notify
    chans = []
    if n.serverchan_key:
        chans.append("ServerChan")
    if n.bark_url:
        chans.append("Bark")
    if n.telegram_token and n.telegram_chat:
        chans.append("Telegram")
    if n.webhook:
        chans.append("Webhook")
    if n.email and n.email.get("to"):
        chans.append("Email")
    if chans:
        rep.ok("通知", "已配置 " + "、".join(chans))
    else:
        rep.warn("通知", "只会在控制台/日志里提示", "抢到票却没人知道最难受；配一个 Bark 或 ServerChan")
    if n.sound:
        rep.ok("通知", "抢到票会响铃")


def check_store(rep: Report):
    from .store import stats
    try:
        s = stats()
        rep.ok("账本", f"历史下单 {s['total']} 次，成功 {s['success']} 次，成功率 {s['rate']}%")
    except Exception as e:
        rep.warn("账本", f"数据库读写异常: {e}")


# ------------------------------------------------------------------ 入口
def run(task_ids: Optional[List[str]] = None) -> Report:
    rep = Report()
    Log.info("开始体检…")
    tasks = check_config(rep, task_ids)
    sess = Session("doctor")
    try:
        check_network(rep, sess)
        if tasks:
            check_routes(rep, sess, tasks)
            check_sale(rep, sess, tasks)
        check_login(rep, sess)
        check_cdn(rep)
        check_notify(rep)
        check_store(rep)
    finally:
        try:
            sess.close()
        except Exception:
            pass
    return rep


def main(task_ids: Optional[List[str]] = None, as_dict: bool = False) -> int:
    rep = run(task_ids)
    if as_dict:
        return rep.to_dict()
    print(rep.render())
    return 0 if rep.verdict() != "fail" else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
