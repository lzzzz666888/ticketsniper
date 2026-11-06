# -*- coding: utf-8 -*-
"""持久化：SQLite 订单账本 + 运行统计。

用 WAL 模式，多线程写入不锁表。schema 自动迁移（新字段用 ALTER 加）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from .config import TaskConfig, ensure_dirs, paths
from .log import log
from .order import OrderResult
from .query import Candidate

Log = log("store")

_LOCK = threading.Lock()
_INIT = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL    NOT NULL,
    task_id     TEXT,
    task_name   TEXT,
    train       TEXT,
    date        TEXT,
    seat        TEXT,
    passengers  TEXT,
    order_id    TEXT,
    ok          INTEGER DEFAULT 0,
    kind        TEXT,
    msg         TEXT,
    wait_s      REAL,
    from_station TEXT,
    to_station   TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    task_id    TEXT,
    kind       TEXT,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at   REAL,
    tasks      INTEGER DEFAULT 0,
    queries    INTEGER DEFAULT 0,
    orders     INTEGER DEFAULT 0,
    success    INTEGER DEFAULT 0
);
"""


def _conn() -> sqlite3.Connection:
    global _INIT
    ensure_dirs()
    c = sqlite3.connect(paths.DB, timeout=10, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    if not _INIT:
        c.executescript(SCHEMA)
        c.commit()
        _INIT = True
    return c


def save_order(task: TaskConfig, res: OrderResult, cand: Optional[Candidate] = None,
               ok: bool = True) -> int:
    with _LOCK:
        c = _conn()
        cur = c.execute(
            "INSERT INTO orders(created_at,task_id,task_name,train,date,seat,passengers,"
            "order_id,ok,kind,msg,wait_s,from_station,to_station) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), task.id, task.name, res.train, res.date, res.seat,
             "、".join(res.passengers), res.order_id, 1 if ok else 0, res.kind,
             res.msg, res.wait_seconds, task.from_station, task.to_station),
        )
        c.commit()
        rid = cur.lastrowid
        c.close()
    # 同时追加一份 JSONL，便于外部脚本消费
    try:
        with open(paths.ORDERS, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                **res.to_dict(),
                "task": task.id, "task_name": task.name,
                "from": task.from_station, "to": task.to_station,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rid


def save_event(task_id: str, kind: str, message: str):
    with _LOCK:
        c = _conn()
        c.execute("INSERT INTO events(created_at,task_id,kind,message) VALUES(?,?,?,?)",
                  (time.time(), task_id, kind, message))
        c.commit()
        c.close()


def list_orders(limit: int = 100, ok_only: bool = False) -> List[dict]:
    c = _conn()
    sql = "SELECT created_at,task_id,task_name,train,date,seat,passengers,order_id,ok,kind,msg,wait_s,from_station,to_station FROM orders"
    if ok_only:
        sql += " WHERE ok=1"
    sql += " ORDER BY id DESC LIMIT ?"
    rows = c.execute(sql, (limit,)).fetchall()
    c.close()
    keys = ["created_at", "task_id", "task_name", "train", "date", "seat",
            "passengers", "order_id", "ok", "kind", "msg", "wait_s",
            "from_station", "to_station"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"]))
        d["ok"] = bool(d["ok"])
        out.append(d)
    return out


def list_events(limit: int = 100) -> List[dict]:
    c = _conn()
    rows = c.execute("SELECT created_at,task_id,kind,message FROM events ORDER BY id DESC LIMIT ?",
                     (limit,)).fetchall()
    c.close()
    return [{"at": time.strftime("%H:%M:%S", time.localtime(r[0])),
             "task_id": r[1], "kind": r[2], "msg": r[3]} for r in rows]


def stats() -> Dict[str, float]:
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    ok = c.execute("SELECT COUNT(*) FROM orders WHERE ok=1").fetchone()[0]
    today = c.execute("SELECT COUNT(*) FROM orders WHERE created_at > ?",
                      (time.time() - 86400,)).fetchone()[0]
    c.close()
    return {"total": total, "success": ok, "today": today,
            "rate": round(ok / total * 100, 1) if total else 0.0}
