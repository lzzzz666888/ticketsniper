# -*- coding: utf-8 -*-
"""抢票执行器：任务状态机 + 查询循环 + 下单竞速。

对比老项目：
- py12306 用「任务线程 + 集群」两层，状态散落在类属性里；这里是明确的状态机，
  每个任务的状态、命中次数、下次可查时间都能被 Web 直接读走。
- testerSunshine 是单线程 while 循环刷全部任务；这里每任务独立线程 + 全局令牌桶，
  慢任务不拖快任务，且总 QPS 可控不会被风控。
- 命中后先本地「去重锁」(同一车次席别当天只允许一个线程下单)，再走 order 链路，
  避免多 session 同时抢同一张票互相触发 dup_order。
"""
from __future__ import annotations

import random
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .api.constants import SEATS, seat_of
from .config import (Config, Passenger, TaskConfig, config, ensure_dirs, paths,
                     store)
from .log import log
from .login import LoginError, LoginManager, prepare_session
from .net import Resp, Session, limiter
from .order import Candidate, OrderResult, Orderer, resolve_choose_seats
from .query import (Candidate, Query, QueryError, Train, build_seats,
                    expand_dates, filter_trains, pick_candidates,
                    resolve_station_codes)
from .sale import Arm, SaleTimeBook

PRESALE_HINT = "15 天（含当天）"

Log = log("worker")


# ---------------------------------------------------------------- 状态
IDLE = "idle"
LOGIN = "login"
QUERY = "query"
HIT = "hit"
QUEUE = "queue"
SUCCESS = "success"
FAILED = "failed"
STOPPED = "stopped"


@dataclass
class TaskState:
    id: str
    name: str = ""
    state: str = IDLE
    from_code: str = ""
    to_code: str = ""
    dates: List[str] = field(default_factory=list)
    round: int = 0                 # 已完成查询轮次
    queries: int = 0               # 查询请求数
    hits: int = 0                  # 命中次数
    orders: int = 0                # 提交订单尝试次数
    last_train: str = ""
    last_seat: str = ""
    last_msg: str = ""
    last_at: float = 0.0
    error: str = ""
    order_id: str = ""
    started_at: float = 0.0
    # 最近一次看到的可选车次（前端展示）
    preview: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "state": self.state,
            "from_code": self.from_code, "to_code": self.to_code,
            "dates": self.dates, "round": self.round, "queries": self.queries,
            "hits": self.hits, "orders": self.orders,
            "last_train": self.last_train, "last_seat": self.last_seat,
            "last_msg": self.last_msg,
            "last_at": time.strftime("%H:%M:%S", time.localtime(self.last_at)) if self.last_at else "",
            "error": self.error, "order_id": self.order_id,
            "uptime": round(time.time() - self.started_at) if self.started_at else 0,
            "preview": self.preview[:20],
        }


class TaskWorker(threading.Thread):
    """一个任务一个线程。 """

    def __init__(self, task: TaskConfig, runner: "Runner", session: Optional[Session] = None,
                 dry: bool = False):
        super().__init__(name=f"task-{task.id}", daemon=True)
        self.task = task
        self.runner = runner
        self.dry = dry
        self.stop_event = runner.stop_event
        self.state = TaskState(id=task.id, name=task.name)
        self.session = session or Session(f"{task.id}")
        self.query = Query(self.session)
        self.orderer = Orderer(self.session)
        self.login_mgr = LoginManager(self.session)
        self.arm: Optional[Arm] = None
        self._claimed: set = set()
        # 下单重试用的备用会话池（同 cookie，不同连接/不同 CDN 出口）
        # 实测 12306 对同一条连接的高频下单会连续给「排队人数超过上限」，
        # 换一条干净连接往往就过了，所以失败后轮换 session 重试。
        self._spare: List[Session] = []
        self._spare_i = 0

    def _rotate_session(self, kind: str) -> Optional[Session]:
        """下单失败后换一条连接重试；返回新会话（拿不到就返回 None）。

        auth 失败不轮换（cookie 已废，得重新登录）；
        dup_order 不轮换（换什么都没用，得先去处理未支付订单）。
        """
        if kind in ("auth", "dup_order", "sold_out"):
            return None
        limit = max(0, int(config().net.order_workers) - 1)
        if limit <= 0:
            return None
        if len(self._spare) < limit:
            try:
                s = Session(f"{self.task.id}-r{len(self._spare) + 1}")
                s.set_cookie_string(self.session.cookie_string())
                s.is_login = self.session.is_login
                s.username = self.session.username
                s.repeat_submit_token = self.session.repeat_submit_token
                self._spare.append(s)
            except Exception as e:
                Log.debug(f"{self.task.id} 备用会话创建失败: {e}")
                return None
        s = self._spare[self._spare_i % len(self._spare)]
        self._spare_i += 1
        Log.info(f"{self.task.id} 换连接重试（会话 {s.name}）")
        return s

    # ---------------- 主循环
    def run(self):
        st = self.state
        st.started_at = time.time()
        cfg = config()
        try:
            if self.dry:
                Log.warn(f"{self.task.id} 演练模式：只查询不下单")
            else:
                st.state = LOGIN
                if not self.runner.ensure_login(self.session, self.login_mgr):
                    st.state = FAILED
                    st.error = "登录失败"
                    return
            fc, tc = resolve_station_codes(self.task, strict=True)
            st.from_code, st.to_code = fc, tc
            dates = expand_dates(self.task)
            st.dates = dates
            if not dates:
                st.state = FAILED
                st.error = "日期全部超出预售期（15 天）"
                Log.warn(f"{self.task.id} {st.error}")
                return
            Log.ok(f"{self.task.id} {self.task.from_station}→{self.task.to_station} "
                   f"{'/'.join(dates)} 席别 {[s.name for s in build_seats(self.task)]}")
            # 起售时间：12306 每天在车站固定时刻放第 15 天的票，围着这个时刻打才是关键
            try:
                book = SaleTimeBook(self.session).load()
                st_msg = book.of(fc)
                if st_msg:
                    Log.info(f"{self.task.id} 起售时刻 {st_msg}"
                             f"（{book.name_of(fc) or fc}），预售 {PRESALE_HINT}")
            except Exception as e:
                Log.debug(f"{self.task.id} 起售时间查询失败: {e}")

            # 起跑前查一下有没有未完成订单：12306 有未支付订单会直接拒绝新订单
            if not self.dry:
                try:
                    pend = self.orderer.unfinished_orders()
                    if pend:
                        codes = []
                        for o in pend:
                            seq = str(o.get("sequence_no") or o.get("order_id") or "")
                            stn = str(o.get("start_train_date_page") or "")
                            codes.append(f"{seq}({stn})" if stn else seq)
                        Log.warn(f"{self.task.id} 账号有 {len(pend)} 个未完成订单："
                                 f"{'、'.join(codes[:5])}。12306 会拒绝新订单，"
                                 f"请先支付或取消")
                        st.last_msg = f"有 {len(pend)} 个未完成订单待处理"
                except Exception as e:
                    Log.debug(f"{self.task.id} 未完成订单检查失败: {e}")

            st.state = QUERY
            self._loop()
        except QueryError as e:
            st.state = FAILED
            st.error = str(e)
            Log.error(f"{self.task.id} {e}")
        except LoginError as e:
            st.state = FAILED
            st.error = f"登录: {e}"
            Log.error(f"{self.task.id} {e}")
        except Exception as e:
            st.state = FAILED
            st.error = f"{type(e).__name__}: {e}"
            Log.exc(f"{self.task.id} 线程异常")
        finally:
            if st.state not in (SUCCESS, FAILED):
                st.state = STOPPED
            Log.info(f"{self.task.id} 结束 state={st.state} msg={st.error or st.last_msg}")

    def _loop(self):
        st, task, cfg = self.state, self.task, config()
        interval = task.query_interval or cfg.net.query_interval
        attempts = 0
        rounds = 0
        # 放票窗口调度：有起售时刻就围着它打，没有就匀速轮询
        try:
            self.arm = Arm.for_task(task, self.session)
        except Exception as e:
            Log.debug(f"{self.task.id} 放票窗口计算失败: {e}")
        last_phase = ""
        while not self.stop_event.is_set() and not self.runner.stopped:
            if cfg.stop_at and time.strftime("%H:%M") >= cfg.stop_at:
                Log.warn(f"{self.task.id} 到达截止时间 {cfg.stop_at}")
                st.state = STOPPED
                return
            if task.max_attempts and attempts >= task.max_attempts:
                Log.warn(f"{self.task.id} 达到最大尝试次数 {task.max_attempts}")
                st.state = STOPPED
                return
            if self.runner.max_rounds and rounds >= self.runner.max_rounds:
                st.state = STOPPED
                st.last_msg = st.last_msg or "达到轮次上限"
                Log.info(f"{self.task.id} 完成 {rounds} 轮查询，停止")
                return

            # 节奏：放票前预热、放票瞬间突发
            if self.arm is not None:
                ph = self.arm.wait_turn(self.stop_event)
                self.arm.apply_rate()
                if ph != last_phase:
                    last_phase = ph
                    if ph == "warmup":
                        Log.info(f"{self.task.id} 进入预热（距放票 "
                                 f"{self.arm.plan.seconds_to_open():.0f} 秒）")
                    elif ph == "burst":
                        Log.hit(f"{self.task.id} 放票瞬间！切换突发模式 "
                                f"间隔 {self.arm.plan.burst_interval}s 限速 {self.arm.plan.burst_rate}/s")
                if self.stop_event.is_set() or self.runner.stopped:
                    return

            # 登录活性守护
            if not self.dry and (not self.session.is_login
                                 or time.time() - self.session.login_at > cfg.login.relogin_minutes * 60):
                st.state = LOGIN
                if not self.runner.ensure_login(self.session, self.login_mgr, force=True):
                    time.sleep(5)
                    continue
                st.state = QUERY

            dates = [d for d in st.dates] or expand_dates(task)
            if not dates:
                st.state = FAILED
                st.error = "无有效日期"
                return

            got_any = False
            # 多日期并发查询：任务写了 3 个日期时串行查要 3 倍延迟，
            # 而抢票窗口只有几秒。httpx.Client 连接池本身线程安全，
            # 限速桶是全局的，所以并发不会放大对 12306 的压力。
            fetched: Dict[str, List[Train]] = {}
            if len(dates) > 1 and cfg.net.query_workers > 1:
                with ThreadPoolExecutor(max_workers=min(cfg.net.query_workers, len(dates))) as ex:
                    futs = {d: ex.submit(self.query.fetch, d, st.from_code, st.to_code)
                            for d in dates}
                    for d, fu in futs.items():
                        try:
                            fetched[d] = fu.result()
                        except Exception as e:
                            Log.debug(f"{self.task.id} {d} 并发查询失败: {e}")
            for date in dates:
                if self.stop_event.is_set() or self.runner.stopped:
                    return
                try:
                    if date in fetched:
                        trains = fetched[date]
                    else:
                        trains = self.query.fetch(date, st.from_code, st.to_code)
                except QueryError as e:
                    st.last_msg = str(e)
                    Log.debug(f"{self.task.id} 查询失败: {e}")
                    # 风控/限速时退避更久
                    sleep = interval * random.uniform(2.0, 4.0)
                    self._sleep(sleep)
                    continue
                st.queries += 1
                st.round += 1
                trains = filter_trains(trains, task)
                st.preview = [t.to_dict() for t in trains]
                cands = pick_candidates(trains, task)
                if not cands:
                    st.last_msg = f"{date} 无可售"
                    continue
                got_any = True
                st.state = HIT
                st.hits += 1
                st.last_at = time.time()
                cand = cands[0]
                st.last_train = cand.train.code
                st.last_seat = cand.seat.name
                Log.hit(f"{self.task.id} 命中 {cand}  候选 {len(cands)} 个")

                if self.dry:
                    st.last_msg = f"演练：将下单 {cand.train.code} {cand.seat.name}"
                    st.state = QUERY
                    rounds += 1
                    if self.runner.max_rounds and rounds >= self.runner.max_rounds:
                        Log.info(f"{self.task.id} 演练完成 {rounds} 轮")
                        st.state = STOPPED
                        return
                    self._sleep(interval * 2)
                    continue

                ok = self._try_order(cand)
                attempts += 1
                if ok:
                    return
                st.state = QUERY

            rounds += 1
            if not got_any:
                st.state = QUERY
                self._sleep(interval * random.uniform(1.0, 1.6))
            else:
                self._sleep(interval * random.uniform(0.4, 0.9))

    # ---------------- 下单
    def _try_order(self, cand: Candidate) -> bool:
        st = self.state
        key = cand.key
        claim = self.runner.claim(key)
        if not claim:
            Log.debug(f"{self.task.id} {key} 已被其它 worker 抢占，跳过")
            st.last_msg = f"{key} 正在下单中"
            return False
        try:
            pax = self._resolve_passengers()
            if not pax:
                st.state = FAILED
                st.error = "没有可用乘客（请检查 tasks.yaml 或登录后同步）"
                return False
            st.state = QUEUE
            st.orders += 1
            choose = resolve_choose_seats(self.task, cand.seat.code, len(pax))
            if choose:
                Log.info(f"{self.task.id} 选座偏好 {'/'.join(choose)}")
            res = self.orderer.place(cand, self.task, pax, choose_seats=choose)
            st.last_msg = res.msg
            if res.ok:
                st.state = SUCCESS
                st.order_id = res.order_id
                self.runner.on_success(self.task, res, cand)
                return True
            Log.warn(f"{self.task.id} 下单失败 [{res.kind}] {res.msg}")
            if res.kind == "auth":
                self.session.is_login = False
                return False
            if res.kind == "dup_order":
                Log.warn(f"{self.task.id} 存在未完成订单，请先去 12306 支付/取消")
                self._sleep(30)
                return False
            if res.kind == "sold_out":
                return False
            # 排队超限 / 超时 / 未知：换连接再打一次
            alt = self._rotate_session(res.kind)
            if alt is None:
                return False
            st.orders += 1
            alt_orderer = Orderer(alt)
            res2 = alt_orderer.place(cand, self.task, pax, choose_seats=choose)
            st.last_msg = res2.msg
            if res2.ok:
                st.state = SUCCESS
                st.order_id = res2.order_id
                self.runner.on_success(self.task, res2, cand)
                return True
            Log.warn(f"{self.task.id} 换连接后仍失败 [{res2.kind}] {res2.msg}")
            return False
        finally:
            self.runner.release(key)

    def _resolve_passengers(self) -> List[Passenger]:
        """任务里写了 id_no 就直接用；只写名字则从账号乘车人里匹配。

        注意：匹配失败时**不能**把 self.task.passengers 覆盖成空列表，
        否则一次网络抖动就会把任务配置里的乘客名永久抹掉，
        之后每轮都直接判「没有可用乘客」。
        """
        want = [p for p in (self.task.passengers or []) if p.name]
        if not want:
            return []
        missing = [p for p in want if not p.id_no]
        if not missing:
            return want
        try:
            rows = self.orderer.passengers()
        except Exception as e:
            Log.warn(f"同步乘车人失败（沿用配置里的姓名重试）: {str(e)[:80]}")
            rows = []
        if not rows:
            return []
        by_name = {str(r.get("passenger_name", "")).strip(): r for r in rows}
        out: List[Passenger] = []
        for p in want:
            if p.id_no:
                out.append(p)
                continue
            r = by_name.get(p.name)
            if not r:
                Log.warn(f"乘车人 {p.name} 不在账号下（用 `ticketsniper passengers` 看真实姓名）")
                continue
            out.append(Passenger(
                name=p.name,
                id_no=str(r.get("passenger_id_no", "")),
                id_type=str(r.get("passenger_id_type_code", "1")),
                ticket_type=str(r.get("passenger_type", "1")),
                phone=p.phone or str(r.get("mobile_no", "") or ""),
            ))
        if out:
            self.task.passengers = out
        return out

    def _sleep(self, s: float):
        end = time.time() + max(0.05, s)
        while time.time() < end:
            if self.stop_event.is_set() or self.runner.stopped:
                return
            time.sleep(min(0.2, end - time.time()))

    def stop(self):
        self.state.state = STOPPED


# ---------------------------------------------------------------- 调度器
class Runner:
    """管理所有任务线程、登录、去重锁、通知。"""

    def __init__(self, cfg: Optional[Config] = None):
        ensure_dirs()
        self.cfg = cfg or config()
        self.stop_event = threading.Event()
        self.stopped = False
        self.once = False
        self.max_rounds = 0
        self.workers: Dict[str, TaskWorker] = {}
        self.states: Dict[str, TaskState] = {}
        self._claim_lock = threading.Lock()
        self._claims: set = set()
        self._login_lock = threading.Lock()
        self._logged_sessions: Dict[int, bool] = {}
        self.on_success_cb: Optional[Callable] = None
        self.started_at = 0.0

    # ---------------- 登录
    def ensure_login(self, sess: Session, mgr: LoginManager, force: bool = False) -> bool:
        with self._login_lock:
            if not force and sess.is_login:
                return True
            try:
                if mgr.check_alive():
                    sess.is_login = True
                    mgr.refresh_order_context()
                    return True
            except Exception:
                pass
            Log.info(f"会话 {sess.name} 开始登录（mode={self.cfg.login.mode}）")
            ok = mgr.ensure(on_qr=self._on_qr)
            if ok:
                Log.ok(f"会话 {sess.name} 登录成功")
            return ok

    def _on_qr(self, path: str):
        from .login import qr_ascii
        try:
            print(qr_ascii(path))
        except Exception:
            pass
        Log.ok(f"请用 12306 APP 扫码登录：{path}")

    # ---------------- 去重锁
    def claim(self, key: str) -> bool:
        with self._claim_lock:
            if key in self._claims:
                return False
            self._claims.add(key)
            return True

    def release(self, key: str):
        with self._claim_lock:
            self._claims.discard(key)

    def claims_active(self) -> bool:
        with self._claim_lock:
            return bool(self._claims)

    # ---------------- 生命周期
    def start(self, task_ids: Optional[Sequence[str]] = None, dry: bool = False):
        store.load()
        tasks = [t for t in store.tasks if t.enabled and (not task_ids or t.id in task_ids)]
        if not tasks:
            Log.warn("没有启用的任务，请编辑 tasks.yaml")
            return 0
        self.started_at = time.time()
        self.stopped = False
        self.stop_event.clear()
        for t in tasks:
            w = TaskWorker(t, self, dry=dry)
            self.workers[t.id] = w
            self.states[t.id] = w.state
            w.start()
            time.sleep(0.4)   # 错开启动，避免瞬时并发登录
        Log.ok(f"已启动 {len(tasks)} 个任务{'（演练）' if dry else ''}")
        return len(tasks)

    def stop(self, wait: float = 3.0):
        self.stopped = True
        self.stop_event.set()
        for w in self.workers.values():
            w.stop()
        end = time.time() + wait
        for w in self.workers.values():
            w.join(timeout=max(0.1, end - time.time()))
        Log.info("所有任务已停止")

    def wait(self):
        for w in list(self.workers.values()):
            w.join()
        self.stopped = True

    def running(self) -> bool:
        return any(w.is_alive() for w in self.workers.values())

    def snapshot(self) -> dict:
        return {
            "running": self.running(),
            "uptime": round(time.time() - self.started_at) if self.started_at else 0,
            "tasks": [w.state.to_dict() for w in self.workers.values()],
            "net": {
                "workers": self.cfg.net.query_workers,
                "rate": self.cfg.net.rate_per_sec,
            },
        }

    # ---------------- 成功回调
    def on_success(self, task: TaskConfig, res: OrderResult, cand: Candidate):
        Log.ok(f"🎫 {task.id} 抢票成功 {cand.train.code} {cand.seat.name} "
               f"订单号 {res.order_id}")
        try:
            from .store import save_order
            save_order(task, res, cand)
        except Exception:
            Log.exc("保存订单失败")
        try:
            from .notify import notify_order
            notify_order(task, res, cand)
        except Exception:
            Log.exc("通知失败")
        if self.on_success_cb:
            try:
                self.on_success_cb(task, res, cand)
            except Exception:
                pass
        if task.stop_on_success:
            Log.warn("已抢到票，停止其它任务")
            self.stopped = True
            self.stop_event.set()
