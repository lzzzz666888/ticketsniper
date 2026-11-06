# -*- coding: utf-8 -*-
"""Web 控制台：FastAPI + 单页前端（内嵌，无需构建）。

功能：
  GET  /               控制台页面
  GET  /api/status     运行状态 + 任务快照 + 网络统计
  GET  /api/tasks      任务列表（读 tasks.yaml）
  POST /api/tasks      新增/更新任务
  DELETE /api/tasks/{id}
  POST /api/run        启动抢票（可指定 task ids）
  POST /api/stop       停止
  GET  /api/logs       最近日志（支持 since/limit/tag）
  GET  /api/logs/stream SSE 实时日志
  GET  /api/orders     订单账本
  GET  /api/query      余票预览 ?from=&to=&date=
  GET  /api/qr         扫码登录：返回二维码图和状态
  GET  /api/cdn        CDN 节点池
  POST /api/cdn/refresh  重新探测
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException, Query as Q, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from ..config import (Config, Passenger, TaskConfig, config, ensure_dirs, paths,
                      store)
from ..log import log, recent
from ..net import cdn_pool
from ..query import QueryError, quick_search
from ..store import list_orders, stats as order_stats
from ..worker import Runner

Log = log("web")

app = FastAPI(title="TicketSniper", docs_url="/api/docs", redoc_url=None)

_RUNNER: Optional[Runner] = None
_RUNNER_LOCK = threading.Lock()
_QR: dict = {"png_b64": "", "status": "idle", "at": 0.0, "uuid": ""}


def _runner() -> Runner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = Runner()
        return _RUNNER


# ---------------------------------------------------------------- 鉴权
def _auth(request: Request):
    token = config().web.token
    if not token:
        return
    got = request.headers.get("x-ts-token") or request.query_params.get("token", "")
    if got != token:
        raise HTTPException(401, "token 无效")


# ---------------------------------------------------------------- 页面
@app.get("/", response_class=HTMLResponse)
def index():
    return index_html()


# ---------------------------------------------------------------- 状态
@app.get("/api/status")
def api_status(request: Request):
    _auth(request)
    r = _runner()
    cfg = config()
    if not cdn_pool.nodes:      # 首次访问时把 data/cdn.txt 载进来，免得显示 0 个节点
        try:
            cdn_pool.load()
        except Exception:
            pass
    return {
        "version": "1.0.0",
        "running": r.running(),
        "tasks": r.snapshot()["tasks"] if r.workers else [],
        "net": {
            "rate": cfg.net.rate_per_sec, "workers": cfg.net.query_workers,
            "cdn_nodes": len(cdn_pool.nodes), "use_cdn": cfg.net.use_cdn,
        },
        "login": {"mode": cfg.login.mode, "logged_in": any(w.session.is_login for w in r.workers.values())},
        "orders": order_stats(),
        "server_time": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------- 任务
@app.get("/api/tasks")
def api_tasks(request: Request):
    _auth(request)
    store.load()
    return {"tasks": [t.__dict__ | {"passengers": [p.__dict__ for p in t.passengers]}
                      for t in store.tasks]}


@app.post("/api/tasks")
def api_task_upsert(request: Request, payload: dict = Body(...)):
    _auth(request)
    store.load()
    tid = payload.get("id") or f"T{len(store.tasks) + 1:02d}"
    pax = [Passenger(**p) if isinstance(p, dict) else Passenger(name=str(p))
           for p in (payload.get("passengers") or [])]
    data = {k: v for k, v in payload.items() if k in TaskConfig.__dataclass_fields__}
    data["id"] = tid
    data["passengers"] = pax
    if not data.get("dates"):
        data["dates"] = ["+1"]
    if not data.get("name"):
        data["name"] = f"{data.get('from_station', '')}→{data.get('to_station', '')} {','.join(data['dates'])}"
    task = TaskConfig(**data)
    store.upsert(task)
    Log.ok(f"保存任务 {tid}")
    return {"ok": True, "id": tid}


@app.delete("/api/tasks/{tid}")
def api_task_delete(tid: str, request: Request):
    _auth(request)
    store.load()
    before = len(store.tasks)
    store.tasks = [t for t in store.tasks if t.id != tid]
    if len(store.tasks) == before:
        raise HTTPException(404, "任务不存在")
    store.save()
    return {"ok": True}


# ---------------------------------------------------------------- 运行控制
@app.post("/api/run")
def api_run(request: Request, payload: dict = Body(default={})):
    _auth(request)
    ids = payload.get("ids") or None
    dry = bool(payload.get("dry"))
    r = _runner()
    if r.running():
        return {"ok": False, "msg": "已在运行中"}
    n = r.start(ids, dry=dry)
    return {"ok": n > 0, "started": n, "dry": dry}


@app.post("/api/stop")
def api_stop(request: Request):
    _auth(request)
    r = _runner()
    r.stop()
    return {"ok": True}


# ---------------------------------------------------------------- 日志
@app.get("/api/logs")
def api_logs(request: Request, limit: int = 200, tag: str = "", level: str = "",
             since: float = 0.0):
    _auth(request)
    return {"logs": recent(limit, level=level or None, tag=tag or None, since_ts=since)}


@app.get("/api/logs/stream")
async def api_logs_stream(request: Request, token: str = ""):
    if config().web.token and token != config().web.token:
        raise HTTPException(401, "token 无效")

    async def gen():
        last = time.time()
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                break
            items = recent(200, since_ts=last)
            if items:
                last = items[-1]["ts"]
                payload = json.dumps(items, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(0.8)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 订单
@app.get("/api/orders")
def api_orders(request: Request, limit: int = 100, ok_only: bool = False):
    _auth(request)
    return {"orders": list_orders(limit, ok_only), "stats": order_stats()}


# ---------------------------------------------------------------- 余票
@app.get("/api/query")
def api_query(request: Request, frm: str = Q("", alias="from"), to: str = "",
              date: str = "", seat: str = ""):
    _auth(request)
    if not (frm and to and date):
        raise HTTPException(400, "需要 from/to/date")
    try:
        trains = quick_search(frm, to, date, seat or "二等座")
    except QueryError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"count": len(trains), "trains": [t.to_dict() for t in trains]}


# ---------------------------------------------------------------- 体检
@app.get("/api/doctor")
def api_doctor(request: Request, tasks: str = ""):
    """开抢前体检：网络/余票路由/起售/登录/CDN/通知。Web 上一键看红绿灯。"""
    _auth(request)
    from ..doctor import run as doctor_run

    ids = [x.strip() for x in tasks.split(",") if x.strip()] or None
    rep = doctor_run(ids)
    return rep.to_dict()


# ---------------------------------------------------------------- 扫码登录
@app.get("/api/sale")
def api_sale(request: Request, station: str = "", date: str = ""):
    """起售时刻 / 放票倒计时。station 支持中文名、电报码、拼音。

    返回两组时间，前端别混用：
    - next_open / countdown       → 这个站「下一次每日放票」是什么时候
    - open_at / open_countdown    → 你查的这条乘车日期的票，什么时候放 / 已放多久
    """
    _auth(request)
    from datetime import datetime

    from ..api.station import book
    from ..sale import PRESALE_DAYS, SaleTimeBook
    code = book.code(station) if station else ""
    if not code:
        raise HTTPException(400, f"站名无法识别: {station}")
    bk = SaleTimeBook().load()
    now = datetime.now()
    out = {
        "station": bk.name_of(code) or station,
        "code": code,
        "sale_time": bk.of(code),
        "presale_days": PRESALE_DAYS,
        "next_open": None,
        "countdown": None,
        "date": date,
        "open_at": None,
        "open_countdown": None,
        "released": None,
    }
    nxt = bk.next_open(code)
    if nxt:
        out["next_open"] = nxt.strftime("%Y-%m-%d %H:%M")
        out["countdown"] = int((nxt - now).total_seconds())
    if date:
        od = bk.open_datetime(code, date)
        if od:
            left = int((od - now).total_seconds())
            out["open_at"] = od.strftime("%Y-%m-%d %H:%M")
            out["open_countdown"] = left
            out["released"] = left <= 0
    return out


@app.get("/api/qr")
def api_qr(request: Request):
    _auth(request)
    return {"png": _QR["png_b64"], "status": _QR["status"], "at": _QR["at"]}


@app.post("/api/login/qr")
def api_login_qr(request: Request):
    """后台线程开一个扫码会话，二维码推送到 /api/qr。"""
    _auth(request)
    if _QR["status"] == "waiting":
        return {"ok": True, "msg": "已在进行中"}

    def work():
        from ..login import LoginManager, qr_ascii, save_qr
        from ..net import Session
        s = Session("web-login")
        mgr = LoginManager(s)
        _QR.update({"status": "waiting", "at": time.time()})

        def on_qr(path: str):
            try:
                with open(path, "rb") as f:
                    _QR["png_b64"] = base64.b64encode(f.read()).decode()
            except Exception:
                pass
            _QR["status"] = "waiting"

        try:
            ok = mgr.login_by_qr(timeout=180, on_qr=on_qr)
            _QR["status"] = "ok" if ok else "fail"
            if ok:
                mgr.activate()
                mgr.refresh_order_context()
                s.save_cookies()
                Log.ok("Web 扫码登录成功，cookie 已保存")
        except Exception as e:
            _QR["status"] = f"err:{e}"
            Log.error(f"扫码登录失败: {e}")
        finally:
            s.close()

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- CDN
@app.get("/api/cdn")
def api_cdn(request: Request):
    _auth(request)
    return {"nodes": [{"ip": n.ip, "rtt": round(n.rtt, 3), "fails": n.fails}
                      for n in sorted(cdn_pool.nodes, key=lambda x: x.score)[:40]]}


@app.post("/api/cdn/refresh")
def api_cdn_refresh(request: Request):
    _auth(request)

    def work():
        n = cdn_pool.refresh()
        Log.ok(f"CDN 刷新完成，可用 {len(n)} 个")

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- 启动
def serve(host: str = None, port: int = None):
    import uvicorn
    c = config()
    h = host or c.web.host
    p = int(port or c.web.port)
    Log.ok(f"控制台 http://{h}:{p}")
    uvicorn.run(app, host=h, port=p, log_level="warning")


# ---------------------------------------------------------------- 前端
_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
_INDEX_CACHE = {"mtime": 0.0, "html": ""}


def index_html() -> str:
    """读 web/index.html，按 mtime 热加载（改前端不用重启）。"""
    try:
        m = os.path.getmtime(_INDEX_PATH)
        if m != _INDEX_CACHE["mtime"]:
            with open(_INDEX_PATH, encoding="utf-8") as f:
                _INDEX_CACHE["html"] = f.read()
            _INDEX_CACHE["mtime"] = m
    except Exception as e:
        Log.warn(f"读取前端失败: {e}")
        return f"<h1>前端缺失</h1><pre>{e}</pre>"
    return _INDEX_CACHE["html"]
