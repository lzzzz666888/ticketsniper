# -*- coding: utf-8 -*-
"""网络层：限速器、CDN 优选、带重试的会话封装。

设计要点（v2 相比老一代 py12306/testerSunshine 的改进）：
1. 单例 httpx.Client + 连接池复用，不做 per-request 新建 session；
2. 令牌桶限速，避免"多线程打满"触发 12306 的频率风控；
3. CDN 优选：并发探测候选 edge IP，按 RTT 排序，失败自动降级到直连；
4. 统一错误归类（auth / risk / sold_out / dup_order / time_out），
   上层只按类别决策，不解析中文字符串。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
import urllib3

from .api.constants import BASE, HOST, ERR_KEYWORDS, URLS
from .config import config, paths

urllib3.disable_warnings()

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]


# ---------------------------------------------------------------- 限速
class RateLimiter:
    """令牌桶。rate=每秒补充的令牌数，burst=桶容量。"""

    def __init__(self, rate: float, burst: int = 1):
        self.rate = max(0.1, float(rate))
        self.capacity = max(1, int(burst))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0, timeout: float = 30.0) -> float:
        """取 n 个令牌，返回实际等待秒数。"""
        deadline = time.monotonic() + timeout
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return waited
                need = (n - self._tokens) / self.rate
            if time.monotonic() + need > deadline:
                time.sleep(min(need, 0.25))
                waited += min(need, 0.25)
                if time.monotonic() > deadline:
                    return waited
                continue
            time.sleep(need)
            waited += need

    def retune(self, rate: float, burst: Optional[int] = None):
        with self._lock:
            self.rate = max(0.1, float(rate))
            if burst:
                self.capacity = max(1, int(burst))
            self._tokens = min(self._tokens, float(self.capacity))


_LIMITER: Optional[RateLimiter] = None
_LIMITER_LOCK = threading.Lock()


def limiter() -> RateLimiter:
    global _LIMITER
    with _LIMITER_LOCK:
        if _LIMITER is None:
            c = config().net
            _LIMITER = RateLimiter(c.rate_per_sec, c.burst)
        return _LIMITER


# ---------------------------------------------------------------- CDN 优选
@dataclass
class CdnNode:
    ip: str
    rtt: float = 9.9
    fails: int = 0
    last_ok: float = 0.0

    @property
    def score(self) -> float:
        # RTT 越低越好，失败次数重罚
        return self.rtt + self.fails * 0.8


class CdnPool:
    """候选 edge IP 池。

    来源三路：
      1) 用户 data/cdn.txt（每行一个 IP 或 IP:PORT）
      2) DoH 多解析器取不同 edge IP（阿里 / 腾讯 / Cloudflare / Google）
      3) 本地已有缓存 data/cdn_ok.json
    探测：GET https://IP/otn/dynamicJs/omseuuq，Host: kyfw.12306.cn
    """

    DOH = [
        ("https://223.5.5.5/resolve", "name={name}&type=A"),
        ("https://doh.pub/dns-query", None),               # 走 httpx 的 JSON 不支持，跳过
        ("https://cloudflare-dns.com/dns-query", "name={name}&type=A"),
        ("https://dns.google/resolve", "name={name}&type=A"),
    ]

    def __init__(self):
        self.nodes: List[CdnNode] = []
        self._lock = threading.RLock()
        self._last_refresh = 0.0
        self.current: Optional[str] = None
        self._rr = 0

    # ---------------- 载入
    def load(self) -> List["CdnNode"]:
        with self._lock:
            self.nodes = []
            f = os.path.join(paths.DATA, "cdn.txt")
            if os.path.exists(f):
                for line in open(f, encoding="utf-8"):
                    ip = line.strip()
                    if ip and not ip.startswith("#"):
                        self.nodes.append(CdnNode(ip))
            if os.path.exists(paths.CDN_CACHE):
                try:
                    data = json.load(open(paths.CDN_CACHE, encoding="utf-8"))
                    for ip, rtt in data.get("items", []):
                        if not any(n.ip == ip for n in self.nodes):
                            self.nodes.append(CdnNode(ip, float(rtt)))
                except Exception:
                    pass
            self._loaded = True
            return self.nodes

    def fetch_edges(self) -> List[str]:
        """DoH 拉取多个 edge IP（不同解析器/不同 ECS 会返回不同节点）。"""
        import httpx as _hx
        ips: List[str] = []
        for url, q in self.DOH:
            if not q:
                continue
            try:
                r = _hx.get(url + "?" + q.format(name=HOST), timeout=6,
                            headers={"accept": "application/dns-json"})
                j = r.json()
                for a in j.get("Answer", []):
                    if a.get("type") == 1:
                        ips.append(a["data"])
            except Exception:
                continue
        # 随机顺序 + 去重
        random.shuffle(ips)
        return list(dict.fromkeys(ips))

    def probe(self, ip: str, timeout: float = 3.0) -> Optional[float]:
        """返回 RTT（秒）；不可用返回 None。

        探针用 dynamicJs（空 body 但 200 即代表 edge 可用）；
        空 body 也算通，早期版本判 len>0 会全部误杀。
        """
        url = URLS["cdn_probe"].format(host=ip)
        t0 = time.monotonic()
        try:
            r = httpx.get(url, headers={"Host": HOST, "User-Agent": UA_LIST[0]},
                          timeout=timeout, verify=False)
            if r.status_code == 200:
                return time.monotonic() - t0
        except Exception:
            return None
        return None

    def refresh(self, workers: int = 12, limit: int = 60, progress=None) -> List[CdnNode]:
        """刷新池子：取候选 → 并发探测 → 按 RTT 排序落盘。"""
        from concurrent.futures import ThreadPoolExecutor
        cands = [n.ip for n in self.nodes] + self.fetch_edges()
        cands = list(dict.fromkeys(cands))[:limit]
        if not cands:
            return []
        results: List[CdnNode] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.probe, ip): ip for ip in cands}
            for fut in futs:
                ip = futs[fut]
                try:
                    rtt = fut.result()
                except Exception:
                    rtt = None
                if progress:
                    progress(ip, rtt)
                if rtt is not None:
                    results.append(CdnNode(ip, rtt, 0, time.time()))
        results.sort(key=lambda n: n.rtt)
        with self._lock:
            old = {n.ip: n for n in self.nodes}
            for n in results:
                if n.ip in old:
                    n.fails = old[n.ip].fails
            self.nodes = results or self.nodes
            self._last_refresh = time.time()
            self.current = results[0].ip if results else None
        self.save()
        return results

    def save(self):
        try:
            json.dump({"items": [[n.ip, round(n.rtt, 3)] for n in self.nodes],
                       "at": int(time.time())},
                      open(paths.CDN_CACHE, "w", encoding="utf-8"))
        except Exception:
            pass

    # ---------------- 取用
    def pick(self) -> Optional[str]:
        c = config().net
        if not c.use_cdn:
            return None
        with self._lock:
            if not self.nodes:
                return None
            if time.time() - self._last_refresh > c.cdn_refresh_minutes * 60:
                # 后台刷新，不阻塞
                threading.Thread(target=self.refresh, daemon=True).start()
            top = sorted(self.nodes, key=lambda n: n.score)[:3]
            n = top[self._rr % len(top)]
            self._rr += 1
            self.current = n.ip
            return n.ip

    def penalize(self, ip: str):
        with self._lock:
            for n in self.nodes:
                if n.ip == ip:
                    n.fails += 1
            if self.current == ip:
                self.current = None


cdn_pool = CdnPool()


# ---------------------------------------------------------------- 响应包装
@dataclass
class Resp:
    status: int = 0
    text: str = ""
    json_data: Optional[dict] = None
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    elapsed: float = 0.0
    error: str = ""
    via: str = "direct"

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    def get(self, path: str, default=None):
        """支持 'data.submitStatus' / 'data.orders[0].id' 这种点路径。"""
        cur = self.json_data if self.json_data is not None else {}
        for part in path.split("."):
            if not part:
                continue
            idx = None
            if part.endswith("]") and "[" in part:
                part, _, idxs = part.partition("[")
                idx = int(idxs[:-1])
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part, default)
            if idx is not None:
                if isinstance(cur, list) and len(cur) > idx:
                    cur = cur[idx]
                else:
                    return default
        return cur

    @property
    def message(self) -> str:
        """提取业务提示文本。"""
        for k in ("messages", "message", "result_message", "errMsg", "data.errMsg"):
            v = self.get(k)
            if isinstance(v, list) and v:
                return str(v[0])
            if isinstance(v, str) and v:
                return v
        return ""


def classify(resp: Resp) -> str:
    """错误归类，返回 ok / auth / risk / sold_out / dup_order / time_out / not_open / queue_full / http / unknown"""
    if resp.error:
        return "http"
    if resp.status != 200:
        return "http"
    text = (resp.message or "") + resp.text[:400]
    jd = resp.json_data or {}
    # 明确成功
    if jd.get("status") is True or jd.get("data") is True:
        return "ok"
    for cat, kws in ERR_KEYWORDS.items():
        for kw in kws:
            if kw and kw in text:
                return cat
    if jd.get("status") is False or jd.get("success") is False:
        return "unknown"
    return "ok"


# ---------------------------------------------------------------- 会话
class Session:
    """一个 12306 会话 = 一套 cookie + 一个连接池。

    多 worker 时每个 worker 独立 Session（cookie 独立），共享 CDN 池与限速器。
    """

    def __init__(self, name: str = "s0", proxy: Optional[str] = None,
                 cookie_file: Optional[str] = None, use_cdn: bool = True):
        c = config().net
        self.name = name
        self.cfg = c
        self.proxy = proxy if proxy is not None else (c.proxy or None)
        self.http = self._build_client()
        self.ua = random.choice(UA_LIST)
        self.cookie_file = cookie_file or paths.COOKIE_JAR
        self.use_cdn = use_cdn and c.use_cdn
        self.stats = {"req": 0, "err": 0, "cdn": 0, "direct": 0}
        self._lock = threading.Lock()
        # 12306 订单页需要的令牌，登录/进入订单页后刷新
        self.repeat_submit_token: str = ""
        self.global_repeat_submit_token: str = ""
        self.ticket_info: dict = {}
        self.passenger_dtos: List[dict] = []
        self.is_login = False
        self.login_at = 0.0
        self.username: str = ""
        self._prefaced_login = False
        # 复用上次保存的 cookie（登录态、设备指纹），避免每次冷启动重新扫码
        self.load_cookies()

    def _build_client(self) -> httpx.Client:
        c = self.cfg
        kw = dict(
            timeout=httpx.Timeout(c.timeout, connect=min(4.0, c.timeout)),
            verify=c.verify_ssl,
            follow_redirects=True,
            http2=bool(c.http2),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={
                "User-Agent": random.choice(UA_LIST),
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": BASE,
                "Referer": BASE + "/otn/leftTicket/init",
                "Connection": "keep-alive",
            },
        )
        if self.proxy:
            kw["proxy"] = self.proxy
        return httpx.Client(**kw)

    # ---------------- cookie
    def load_cookies(self):
        if not os.path.exists(self.cookie_file):
            return
        try:
            data = json.load(open(self.cookie_file, encoding="utf-8"))
            for k, v in data.items():
                self.http.cookies.set(k, v, domain=HOST)
        except Exception:
            pass

    # 只把真正属于「登录态」的 cookie 落盘，其余（尤其是会话级的
    # JSESSIONID）绝不跨进程复用 —— 早期版本 close() 无条件写全量 jar，
    # 结果测试时塞进去的假 JSESSIONID/tk 被后续所有进程加载，
    # 表现是查询全部返回 HTML 错误页（「非 JSON 响应」）。
    _AUTH_KEYS = {"tk", "RAIL_DEVICEID", "RAIL_EXPIRATION", "RAIL_CSESSIONID",
                  "RAIL_SESSIONID", "BIGipServerpassport", "route", "uamtk"}

    def save_cookies(self, force: bool = False):
        try:
            if force or self.is_login:
                data = {k: v for k, v in self.http.cookies.items()}
            else:
                # 未登录：只留设备指纹，登录凭证一律不落盘
                data = {k: v for k, v in self.http.cookies.items() if k in self._AUTH_KEYS}
                data.pop("tk", None)
            json.dump(data, open(self.cookie_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    def cookie_string(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.http.cookies.items())

    def set_cookie_string(self, s: str):
        for part in s.split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                self.http.cookies.set(k, v, domain=HOST)

    # ---------------- 请求
    def request(self, method: str, url: str, *, use_cdn: Optional[bool] = None,
                retries: Optional[int] = None, **kw) -> Resp:
        c = self.cfg
        retries = c.retries if retries is None else retries
        use_cdn = self.use_cdn if use_cdn is None else use_cdn
        last = Resp(url=url)
        for attempt in range(retries + 1):
            limiter().acquire()
            target, headers, via = url, dict(kw.pop("headers", {}) or {}), "direct"
            ip = cdn_pool.pick() if use_cdn and url.startswith("https://kyfw.12306.cn") else None
            if ip:
                target = url.replace("https://" + HOST, "https://" + ip)
                headers["Host"] = HOST
                via = ip
            t0 = time.monotonic()
            try:
                r = self.http.request(method, target, headers=headers,
                                      timeout=httpx.Timeout(c.timeout), **kw)
                el = time.monotonic() - t0
                data = None
                ct = r.headers.get("content-type", "")
                if "json" in ct or r.text[:1] in ("{", "["):
                    try:
                        data = r.json()
                    except Exception:
                        data = None
                last = Resp(r.status_code, r.text, data, dict(r.headers), url, el, "", via)
                self.stats["req"] += 1
                self.stats["cdn" if ip else "direct"] += 1
                if r.status_code == 200:
                    return last
                # 12306 的「路由提示」：302 无 Location，body 是
                # {"c_url":"leftTicket/queryG","c_name":"CLeftTicketUrl","status":false}
                # 上层据此切换入口，不能当成错误
                if r.status_code in (301, 302, 307, 308):
                    if isinstance(data, dict) and data.get("c_url"):
                        last.status = 200
                        last.text = r.text
                        return last
                    loc = r.headers.get("location", "")
                    last = Resp(200, "", {"redirect": loc}, dict(r.headers), url, el, "", via)
                    return last
                # 5xx / 429 → 换 CDN 重试
                if ip:
                    cdn_pool.penalize(ip)
                last.error = f"HTTP {r.status_code}"
            except Exception as e:
                self.stats["err"] += 1
                if ip:
                    cdn_pool.penalize(ip)
                last = Resp(0, "", None, {}, url, time.monotonic() - t0,
                            f"{type(e).__name__}: {str(e)[:80]}", via)
            if attempt < retries:
                time.sleep(min(2.0, 0.25 * (2 ** attempt)) + random.random() * c.jitter)
        return last

    def get(self, url: str, **kw) -> Resp:
        return self.request("GET", url, **kw)

    def post(self, url: str, data=None, **kw) -> Resp:
        return self.request("POST", url, data=data, **kw)

    def preface(self) -> Resp:
        """预热：拿 JSESSIONID + 设备指纹 cookie，任何链路的第一步。"""
        r = self.get(URLS["init"], use_cdn=False)
        return r

    def close(self):
        try:
            self.save_cookies()
            self.http.close()
        except Exception:
            pass
