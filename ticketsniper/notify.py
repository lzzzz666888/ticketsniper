# -*- coding: utf-8 -*-
"""通知渠道：控制台 / Server酱 / Bark / Telegram / 通用 webhook / 邮件 / 声音。

设计成「全部失败也不影响主流程」——通知永远在 try 里跑。
"""
from __future__ import annotations

import json
import os
import smtplib
import threading
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from .config import Config, NotifyConfig, TaskConfig, config
from .log import log
from .order import OrderResult
from .query import Candidate

Log = log("notify")


def _post(url: str, data=None, json_body=None, timeout: float = 8.0) -> str:
    import httpx
    try:
        if json_body is not None:
            r = httpx.post(url, json=json_body, timeout=timeout)
        else:
            r = httpx.post(url, data=data, timeout=timeout)
        return r.text[:300]
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"


def _get(url: str, timeout: float = 8.0) -> str:
    import httpx
    try:
        return httpx.get(url, timeout=timeout).text[:300]
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"


# ---------------------------------------------------------------- 各渠道
def send_console(title: str, body: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("-" * 60)
    for line in body.splitlines():
        print(f"  {line}")
    print("=" * 60 + "\n")


def send_serverchan(key: str, title: str, body: str) -> str:
    """Server酱 Turbo：SCT 开头的 key 用新接口。"""
    if key.startswith("SCT"):
        return _post(f"https://sctapi.ftqq.com/{key}.send",
                     data={"title": title, "desp": body})
    return _post(f"https://sc.ftqq.com/{key}.send",
                 data={"text": title, "desp": body})


def send_bark(url: str, title: str, body: str) -> str:
    """bark_url 形如 https://api.day.app/YOUR_KEY"""
    u = url.rstrip("/")
    payload = {"title": title, "body": body, "group": "抢票", "sound": "bell"}
    return _post(u, json_body=payload)


def send_telegram(token: str, chat: str, title: str, body: str) -> str:
    u = f"https://api.telegram.org/bot{token}/sendMessage"
    return _post(u, json_body={"chat_id": chat, "text": f"*{title}*\n{body}",
                               "parse_mode": "Markdown"})


def send_webhook(url: str, title: str, body: str) -> str:
    return _post(url, json_body={"title": title, "text": body,
                                 "ts": int(time.time())})


def send_email(cfg: Dict, title: str, body: str) -> str:
    host = cfg.get("smtp") or ""
    port = int(cfg.get("port") or 465)
    user = cfg.get("user") or ""
    pwd = cfg.get("password") or os.getenv("TS_MAIL_PASS", "")
    to = cfg.get("to") or user
    if not (host and user and pwd):
        return "email: 缺少 smtp/user/password"
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = user
        msg["To"] = to
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            s = smtplib.SMTP(host, port, timeout=12)
            try:
                s.starttls()
            except Exception:
                pass
        s.login(user, pwd)
        s.sendmail(user, [x.strip() for x in str(to).split(",") if x.strip()], msg.as_string())
        s.quit()
        return "email: ok"
    except Exception as e:
        return f"email ERR {type(e).__name__}: {e}"


def play_sound():
    """Windows 提示音；其它平台尝试 afplay / beep。"""
    try:
        if os.name == "nt":
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 220)
                winsound.Beep(900, 220)
        else:
            os.system("printf '\\a'")
    except Exception:
        pass


# ---------------------------------------------------------------- 汇总
def _pax_text(pax) -> str:
    """res.passengers 可能是列表，也可能是已经拼好的字符串，别按字符拆。"""
    if isinstance(pax, str):
        return pax.replace(", ", "、").replace(",", "、").strip()
    try:
        names = [str(getattr(x, "name", x)) for x in pax]
        return "、".join(n for n in names if n)
    except TypeError:
        return str(pax)


def _format(title_kind: str, task: TaskConfig, res: OrderResult, cand: Optional[Candidate]) -> tuple:
    t = cand.train if cand else None
    title = f"🎫 {title_kind} {task.name or task.id}"
    lines = [
        f"任务: {task.name} ({task.id})",
        f"车次: {t.code if t else res.train}",
        f"区间: {t.from_name + '→' + t.to_name if t else f'{task.from_station}→{task.to_station}'}",
        f"日期: {res.date or (t.date if t else '')}",
        f"时间: {t.start_time + '-' + t.arrive_time if t else ''}",
        f"席别: {res.seat}",
        f"乘客: {_pax_text(res.passengers)}",
        f"订单号: {res.order_id}",
        f"耗时: {res.wait_seconds:.0f}s",
        "",
        "30 分钟内完成支付，否则订单自动取消。",
    ]
    return title, "\n".join(lines)


def notify_order(task: TaskConfig, res: OrderResult, cand: Optional[Candidate] = None,
                 cfg: Optional[Config] = None) -> Dict[str, str]:
    """按配置并发推送，返回各渠道结果。"""
    c = (cfg or config()).notify
    title, body = _format("抢票成功", task, res, cand)
    results: Dict[str, str] = {}
    channels = c.channels or ["console"]

    def run_one(name: str):
        try:
            if name == "console":
                send_console(title, body)
                results[name] = "ok"
            elif name == "serverchan" and c.serverchan_key:
                results[name] = send_serverchan(c.serverchan_key, title, body)
            elif name == "bark" and c.bark_url:
                results[name] = send_bark(c.bark_url, title, body)
            elif name == "telegram" and c.telegram_token:
                results[name] = send_telegram(c.telegram_token, c.telegram_chat, title, body)
            elif name == "webhook" and c.webhook:
                results[name] = send_webhook(c.webhook, title, body)
            elif name == "email" and isinstance(c.email, dict) and c.email:
                results[name] = send_email(c.email, title, body)
            else:
                results[name] = "skip(未配置)"
        except Exception as e:
            results[name] = f"ERR {type(e).__name__}: {e}"

    threads = [threading.Thread(target=run_one, args=(n,), daemon=True) for n in channels]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    if c.sound:
        threading.Thread(target=play_sound, daemon=True).start()
    for k, v in results.items():
        Log.info(f"通知[{k}] → {v}")
    return results


def notify_text(title: str, body: str, cfg: Optional[Config] = None) -> Dict[str, str]:
    """通用推送（用于启动、异常、统计）。"""
    c = (cfg or config()).notify
    results: Dict[str, str] = {}
    for name in (c.channels or ["console"]):
        try:
            if name == "console":
                send_console(title, body)
                results[name] = "ok"
            elif name == "serverchan" and c.serverchan_key:
                results[name] = send_serverchan(c.serverchan_key, title, body)
            elif name == "bark" and c.bark_url:
                results[name] = send_bark(c.bark_url, title, body)
            elif name == "telegram" and c.telegram_token:
                results[name] = send_telegram(c.telegram_token, c.telegram_chat, title, body)
            elif name == "webhook" and c.webhook:
                results[name] = send_webhook(c.webhook, title, body)
        except Exception as e:
            results[name] = f"ERR {e}"
    return results
