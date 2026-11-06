# -*- coding: utf-8 -*-
"""ticketsniper —— 12306 抢票引擎。

模块：
  api/constants  接口与字段常量（升级改这里）
  api/station    站名 ↔ 电报码
  net            HTTP 会话、限速、CDN 优选
  login          扫码/密码/Cookie 登录 + 心跳
  query          余票解析与候选筛选
  order          下单链路
  worker         任务调度与竞速
  store          SQLite 账本
  notify         多渠道推送
  web            FastAPI 控制台
"""
from __future__ import annotations

__version__ = "1.0.0"

from .config import Config, Passenger, TaskConfig, config, ensure_dirs, paths, store
from .log import log, setup as setup_log

__all__ = [
    "Config", "Passenger", "TaskConfig", "config", "ensure_dirs", "paths",
    "store", "log", "setup_log", "__version__",
]
