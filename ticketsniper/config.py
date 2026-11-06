# -*- coding: utf-8 -*-
"""配置层：路径、YAML 读写、任务数据模型。

配置分两块：
  config.yaml  —— 全局（并发、限速、通知、代理、日志）
  tasks.yaml   —— 抢票任务（车次、日期、席别、乘客），支持热重载
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ticketsniper/
DATA = os.path.join(ROOT, "data")
RUNTIME = os.path.join(ROOT, "runtime")
LOGS = os.path.join(RUNTIME, "logs")
SESSION_DIR = os.path.join(RUNTIME, "sessions")


class paths:
    ROOT = ROOT
    DATA = DATA
    RUNTIME = RUNTIME
    LOGS = LOGS
    SESSIONS = SESSION_DIR
    CONFIG = os.path.join(ROOT, "config.yaml")
    TASKS = os.path.join(ROOT, "tasks.yaml")
    STATION_CACHE = os.path.join(DATA, "stations.json")
    CDN_CACHE = os.path.join(DATA, "cdn_ok.json")
    COOKIE_JAR = os.path.join(SESSION_DIR, "cookies.json")
    ORDERS = os.path.join(RUNTIME, "orders.jsonl")
    DB = os.path.join(RUNTIME, "ticketsniper.db")


def ensure_dirs():
    for d in (DATA, RUNTIME, LOGS, SESSION_DIR):
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- 配置模型
@dataclass
class NetConfig:
    timeout: float = 6.0
    retries: int = 3
    verify_ssl: bool = False
    http2: bool = True
    # 全局速率：每秒最多 N 个请求（token bucket）
    rate_per_sec: float = 4.0
    burst: int = 8
    # 并发：同时跑几个查询 worker（每个 worker 一个 session）
    query_workers: int = 3
    # 单任务多 session 竞速下单
    order_workers: int = 2
    # 查询间隔（秒，每个 worker 独立抖动）
    query_interval: float = 0.8
    jitter: float = 0.35
    # 代理，例：http://127.0.0.1:7890；空=直连
    proxy: str = ""
    # 多出口代理池文件（每行一个）
    proxy_file: str = ""
    # 强制走 CDN 优选 IP（Host 头保持 kyfw.12306.cn）
    use_cdn: bool = True
    cdn_refresh_minutes: int = 720


@dataclass
class LoginConfig:
    # password | qrcode | cookie
    mode: str = "qrcode"
    username: str = ""
    password: str = ""          # 建议用环境变量 TS_PASSWORD 覆盖
    # 手动 cookie 字符串（浏览器 F12 复制）
    cookie: str = ""
    # 登录后 90 分钟自动重登
    relogin_minutes: int = 90
    # 滑块：browser | manual | skip
    slide: str = "browser"


@dataclass
class NotifyConfig:
    # console | serverchan | bark | telegram | webhook | email
    channels: List[str] = field(default_factory=lambda: ["console"])
    serverchan_key: str = ""
    bark_url: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    webhook: str = ""
    email: Dict[str, Any] = field(default_factory=dict)
    # 抢票成功时播放提示音（Windows）
    sound: bool = True


@dataclass
class WebConfig:
    enable: bool = True
    host: str = "127.0.0.1"
    port: int = 8848
    # 只监听本机时留空；对外开放请设置
    token: str = ""


@dataclass
class Config:
    net: NetConfig = field(default_factory=NetConfig)
    login: LoginConfig = field(default_factory=LoginConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    web: WebConfig = field(default_factory=WebConfig)
    # 抢到后是否自动支付（12306 无开放支付接口，保持 False 只会到"待支付"）
    auto_pay: bool = False
    # 全局截止时间：到点停止所有任务
    stop_at: str = ""
    # 日志级别
    log_level: str = "INFO"
    # 是否记录每次请求（调试用，量大）
    trace_http: bool = False

    # ---------------- 读写
    @classmethod
    def load(cls, path: str = None) -> "Config":
        p = path or paths.CONFIG
        cfg = cls()
        if os.path.exists(p):
            raw = yaml.safe_load(open(p, encoding="utf-8")) or {}
            for k, v in raw.items():
                if not hasattr(cfg, k):
                    continue
                cur = getattr(cfg, k)
                if isinstance(cur, (NetConfig, LoginConfig, NotifyConfig, WebConfig)) and isinstance(v, dict):
                    for kk, vv in v.items():
                        if hasattr(cur, kk) and vv is not None:
                            setattr(cur, kk, vv)
                else:
                    setattr(cfg, k, v)
        # 环境变量覆盖敏感项
        cfg.login.password = os.getenv("TS_PASSWORD") or cfg.login.password
        cfg.login.cookie = os.getenv("TS_COOKIE") or cfg.login.cookie
        cfg.notify.serverchan_key = os.getenv("TS_SCKEY") or cfg.notify.serverchan_key
        cfg.web.token = os.getenv("TS_WEB_TOKEN") or cfg.web.token
        return cfg

    def dump(self, path: str = None):
        p = path or paths.CONFIG
        data = {
            "net": asdict(self.net),
            "login": asdict(self.login),
            "notify": asdict(self.notify),
            "web": asdict(self.web),
            "auto_pay": self.auto_pay,
            "stop_at": self.stop_at,
            "log_level": self.log_level,
            "trace_http": self.trace_http,
        }
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, p)


# ---------------------------------------------------------------- 任务模型
@dataclass
class Passenger:
    name: str = ""
    # 身份证号；留空则从 12306 账号里按姓名匹配
    id_no: str = ""
    # 12306 内部 passenger_id_type_code：1=身份证 2=护照 ...
    id_type: str = "1"
    # 票种：1=成人 2=儿童 3=学生 4=残军
    ticket_type: str = "1"
    phone: str = ""

    @property
    def key(self) -> str:
        return f"{self.name}:{self.id_no[-6:] if self.id_no else ''}"


@dataclass
class TaskConfig:
    id: str = ""
    name: str = ""
    enabled: bool = True
    # 出发/到达：站名、电报码、拼音都行
    from_station: str = "北京南"
    to_station: str = "上海虹桥"
    # 日期：YYYY-MM-DD，或区间（起,止），或 +3（今天起 3 天后）
    dates: List[str] = field(default_factory=list)
    # 车次白名单，空=不限
    trains: List[str] = field(default_factory=list)
    # 车次黑名单
    exclude_trains: List[str] = field(default_factory=list)
    # 席别偏好（顺序即优先级）
    seats: List[str] = field(default_factory=lambda: ["二等座"])
    # 出发时间窗 HH:MM-HH:MM，空=不限
    depart_between: str = ""
    # 到达时间窗
    arrive_between: str = ""
    # 只买这些出发站/到达站完全一致的车次（避免买到中途站）
    exact_station: bool = False
    # 乘客
    passengers: List[Passenger] = field(default_factory=list)
    # 每个车次最多尝试下单次数
    max_attempts: int = 0  # 0 = 不限，直到成功或截止
    # 候选席别是否允许降级（二等抢不到自动试一等）
    allow_upgrade: bool = False
    # 是否允许无座
    allow_no_seat: bool = False
    # 是否自动提交候补
    allow_waitlist: bool = False
    # 抢到就停其他任务
    stop_on_success: bool = True
    # 选座偏好：字母（A/B/C/D/F）或中文（靠窗/过道/下铺/中铺/上铺）
    # 例：["F"] 二等座靠窗；["下铺"] 硬卧下铺。留空=系统随机
    seat_prefer: List[str] = field(default_factory=list)
    # 单独限速（覆盖全局 query_interval）
    query_interval: Optional[float] = None

    def resolved_dates(self) -> List[str]:
        """把 dates 里的区间/+N 展开成具体日期列表。"""
        import datetime as dt
        today = dt.date.today()
        out: List[str] = []
        for d in self.dates:
            d = str(d).strip()
            if not d:
                continue
            if d.startswith("+") and d[1:].isdigit():
                out.append((today + dt.timedelta(days=int(d[1:]))).isoformat())
                continue
            if "," in d or "~" in d:
                sep = "," if "," in d else "~"
                a, b = [x.strip() for x in d.split(sep, 1)]
                try:
                    da = dt.date.fromisoformat(a if len(a) == 10 else (today + dt.timedelta(days=int(a))).isoformat())
                    db = dt.date.fromisoformat(b if len(b) == 10 else (today + dt.timedelta(days=int(b))).isoformat())
                except Exception:
                    continue
                cur = da
                while cur <= db:
                    out.append(cur.isoformat())
                    cur += dt.timedelta(days=1)
                continue
            out.append(d)
        # 去重保序
        seen, res = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                res.append(x)
        return res


class TaskStore:
    """tasks.yaml 的读写 + 热重载。"""

    def __init__(self, path: str = None):
        self.path = path or paths.TASKS
        self._lock = threading.RLock()
        self._mtime = 0.0
        self.tasks: List[TaskConfig] = []
        self.version = 0

    def load(self) -> List[TaskConfig]:
        with self._lock:
            if not os.path.exists(self.path):
                self.tasks = []
                return self.tasks
            raw = yaml.safe_load(open(self.path, encoding="utf-8")) or {}
            items = raw.get("tasks", raw if isinstance(raw, list) else [])
            tasks: List[TaskConfig] = []
            for i, t in enumerate(items):
                if not isinstance(t, dict):
                    continue
                pax = [Passenger(**p) if isinstance(p, dict) else Passenger(name=str(p))
                       for p in (t.get("passengers") or [])]
                t = dict(t)
                t["passengers"] = pax
                t.setdefault("id", f"T{i + 1:02d}")
                if not t.get("name"):
                    t["name"] = f"{t.get('from_station', '')}→{t.get('to_station', '')} {','.join(t.get('dates') or [])}"
                if not t.get("dates"):
                    t["dates"] = ["+1"]
                tasks.append(TaskConfig(**{k: v for k, v in t.items() if k in TaskConfig.__dataclass_fields__}))
            self.tasks = tasks
            self._mtime = os.path.getmtime(self.path)
            self.version += 1
            return tasks

    def maybe_reload(self) -> bool:
        with self._lock:
            if not os.path.exists(self.path):
                return False
            m = os.path.getmtime(self.path)
            if m > self._mtime:
                self.load()
                return True
            return False

    def save(self):
        with self._lock:
            data = {"tasks": []}
            for t in self.tasks:
                d = asdict(t)
                data["tasks"].append(d)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
            self.version += 1

    def get(self, tid: str) -> Optional[TaskConfig]:
        with self._lock:
            for t in self.tasks:
                if t.id == tid:
                    return t
            return None

    def upsert(self, task: TaskConfig):
        with self._lock:
            for i, t in enumerate(self.tasks):
                if t.id == task.id:
                    self.tasks[i] = task
                    break
            else:
                self.tasks.append(task)
            self.save()


# ---------------------------------------------------------------- 单例
_CFG: Optional[Config] = None
_CFG_LOCK = threading.Lock()


def config() -> Config:
    global _CFG
    with _CFG_LOCK:
        if _CFG is None:
            ensure_dirs()
            _CFG = Config.load()
        return _CFG


def reload_config() -> Config:
    global _CFG
    with _CFG_LOCK:
        _CFG = Config.load()
        return _CFG


store = TaskStore()


# ---------------------------------------------------------------- 默认文件
DEFAULT_CONFIG_YAML = """\
net:
  timeout: 6.0
  retries: 3
  verify_ssl: false
  http2: true
  rate_per_sec: 4.0
  burst: 8
  query_workers: 3
  order_workers: 2
  query_interval: 0.8
  jitter: 0.35
  proxy: ""
  proxy_file: ""
  use_cdn: true
  cdn_refresh_minutes: 720

login:
  mode: qrcode          # qrcode | password | cookie
  username: ""
  password: ""          # 推荐 export TS_PASSWORD=xxx
  cookie: ""            # 推荐 export TS_COOKIE=xxx
  relogin_minutes: 90
  slide: browser        # browser | manual | skip

notify:
  channels: [console]
  serverchan_key: ""
  bark_url: ""
  telegram_token: ""
  telegram_chat: ""
  webhook: ""
  sound: true

web:
  enable: true
  host: 127.0.0.1
  port: 8848
  token: ""

auto_pay: false
stop_at: ""
log_level: INFO
trace_http: false
"""

DEFAULT_TASKS_YAML = """\
tasks:
  - id: T01
    name: 北京南→上海虹桥 明天
    enabled: true
    from_station: 北京南
    to_station: 上海虹桥
    dates: ["+1"]
    trains: []                 # 空=不限车次
    exclude_trains: []
    seats: [二等座, 一等座]
    depart_between: "06:00-12:00"
    arrive_between: ""
    exact_station: false
    passengers:
      - name: 张三
        id_no: ""              # 留空则用账号下同名乘车人
    max_attempts: 0
    allow_no_seat: false
    allow_waitlist: false
    seat_prefer: []            # 选座偏好：靠窗/F/过道/C/下铺…（仅动车组生效）
    stop_on_success: true
"""


def bootstrap():
    ensure_dirs()
    if not os.path.exists(paths.CONFIG):
        open(paths.CONFIG, "w", encoding="utf-8").write(DEFAULT_CONFIG_YAML)
    if not os.path.exists(paths.TASKS):
        open(paths.TASKS, "w", encoding="utf-8").write(DEFAULT_TASKS_YAML)
