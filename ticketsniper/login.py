# -*- coding: utf-8 -*-
"""登录：扫码 / 账号密码 / 手工 Cookie 三条路，外加心跳重登。

2026-11 线上实测通过的协议顺序（本模块全部按实测结果实现）：

  0. GET  /otn/leftTicket/init            → JSESSIONID / BIGipServerotn
     GET  /otn/resources/login.html       → 建立 Referer 上下文
  1. 扫码（推荐，免验证码、免滑块）
     POST /passport/web/create-qr64  data={appid: 'otn'}
          → {"result_code":"0","image":"<base64 jpg>","uuid":"..."}
     POST /passport/web/checkqr      data={RAIL_DEVICEID, RAIL_EXPIRATION, uuid, appid}
          → result_code: "0" 待扫码 / "1" 已扫码待确认 / "2" 登录成功 / "3" 已失效 / "5" 系统异常
     （状态码语义来自官方 login_new_v20260207.js 的 popup_tipsQrInfo 注释，实测一致：
      二维码 20 秒不扫即转 "3"，此前一直是 "0"）
     注意：checkqr 必须 POST，GET 会 405；RAIL_DEVICEID 传空串也能过。
  2. 密码（有验证码）
     GET  /passport/captcha/captcha-image64 → {"image": base64, "result_code":"0"}
     GET  /passport/captcha/captcha-check?answer=..&_=ts → result_code "4" 表示通过
     POST /passport/web/login  data={
            'loginUserDTO.user_name': 账号,
            'userDTO.password': '@' + SM4-ECB-base64(明文, 'tiekeyuankp12306'),
            'appid': 'otn', 'answer': 验证码点位 }
         实测：字段名必须是 DTO 形式，用 username/password 会返回
         {"result_code":11,"result_message":"请求方式不正确"}。
  3. 换票
     POST /passport/web/auth/uamtk {appid:'otn'} → newapptk
     POST /otn/uamauthclient {tk: newapptk}      → 下发 tk cookie（真正登录态）
  4. GET  /otn/confirmPassenger/initDc → REPEAT_SUBMIT_TOKEN + ticketInfoForPassengerForm
     POST /otn/confirmPassenger/getPassengerDTOs → 乘客列表
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .api.constants import HOST, URLS
from .config import config, paths
from .crypto_sm4 import password_field, self_test as sm4_self_test
from .net import Resp, Session, classify
from .log import log

Log = log("login")

SM4_KEY = "tiekeyuankp12306"
UA_LOGIN_REFERER = "https://kyfw.12306.cn/otn/resources/login.html"


class LoginError(Exception):
    pass


# ---------------------------------------------------------------- 工具
def _qr_raw(b64: str) -> bytes:
    raw = b64.split(",", 1)[-1]
    return base64.b64decode(raw)


def save_qr(b64: str, path: str) -> str:
    """把 base64 二维码落成 png/jpg 文件，返回路径。"""
    data = _qr_raw(b64)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def qr_ascii(path: str, width: int = 41) -> str:
    """终端里直接显示二维码（只依赖 pillow）。"""
    try:
        from PIL import Image
    except Exception:
        return ""
    try:
        img = Image.open(path).convert("L").resize((width, width))
        px = img.load()
        out = []
        for y in range(width):
            out.append("".join("██" if px[x, y] < 128 else "  " for x in range(width)))
        return "\n".join(out)
    except Exception:
        return ""


def gen_device_id() -> str:
    """构造一个形状合法的 RAIL_DEVICEID（服务端未严格校验，仅用于 cookie 补齐）。"""
    seed = f"{random.random()}{time.time()}"
    return base64.urlsafe_b64encode(seed.encode()).decode().rstrip("=")[:64]


class LoginManager:
    """把一个 Session 从匿名态推到登录态，并维持。"""

    def __init__(self, session: Session):
        self.s = session
        self.cfg = config().login
        self._stop = threading.Event()
        self._qr_path = os.path.join(paths.RUNTIME, "login_qr.png")
        self._passport_headers = {
            "Referer": UA_LOGIN_REFERER,
            "Origin": "https://kyfw.12306.cn",
            "X-Requested-With": "XMLHttpRequest",
        }

    # ------------------------------------------------------------ 公共入口
    def prepare(self) -> None:
        """预热：拿 JSESSIONID，登录页 Referer 上下文，补齐设备 cookie。"""
        if getattr(self.s, "_prefaced_login", False):
            return
        self.s.preface()
        self.s.get(UA_LOGIN_REFERER, use_cdn=False)
        try:
            jar = self.s.http.cookies
            if not jar.get("RAIL_DEVICEID"):
                jar.set("RAIL_DEVICEID", gen_device_id(), domain=".12306.cn")
            if not jar.get("RAIL_EXPIRATION"):
                jar.set("RAIL_EXPIRATION", str(int((time.time() + 86400 * 30) * 1000)),
                        domain=".12306.cn")
        except Exception:
            pass
        self.s._prefaced_login = True

    def ensure(self, on_qr: Optional[Callable[[str], None]] = None) -> bool:
        self.prepare()
        mode = (self.cfg.mode or "qrcode").lower()
        if self.cfg.cookie:
            ok = self.login_by_cookie(self.cfg.cookie)
        elif mode == "cookie":
            ok = self.login_by_cookie(self.cfg.cookie or self._cookie_from_jar())
        elif mode == "password":
            ok = self.login_by_password()
        else:
            ok = self.login_by_qr(on_qr=on_qr)
        if ok:
            ok = self.activate()
        return ok

    def _cookie_from_jar(self) -> str:
        return self.s.cookie_string()

    # ------------------------------------------------------------ 扫码
    def create_qr(self) -> Tuple[str, str]:
        """返回 (base64图, uuid)。失败抛 LoginError。"""
        r = self.s.post(URLS["qr_create"], data={"appid": "otn"},
                        headers=self._passport_headers, use_cdn=False)
        code = str(r.get("result_code"))
        img = r.get("image") or r.get("data.image") or ""
        uuid = r.get("uuid") or r.get("data.uuid") or ""
        if code != "0" or not img:
            raise LoginError(f"二维码获取失败: {r.message or r.text[:120]}")
        return img, uuid

    def check_qr(self, uuid: str) -> Tuple[str, str]:
        """返回 (result_code, result_message)。"""
        jar = self.s.http.cookies
        data = {
            "RAIL_DEVICEID": jar.get("RAIL_DEVICEID") or "",
            "RAIL_EXPIRATION": jar.get("RAIL_EXPIRATION") or "",
            "uuid": uuid,
            "appid": "otn",
        }
        r = self.s.post(URLS["qr_check"], data=data,
                        headers=self._passport_headers, use_cdn=False)
        return str(r.get("result_code")), (r.message or "")

    def login_by_qr(self, timeout: float = 300.0,
                    on_qr: Optional[Callable[[str], None]] = None) -> bool:
        self.prepare()
        if not sm4_self_test():
            Log.warning("SM4 自检未通过，密码登录会失败（扫码不受影响）")
        deadline = time.time() + timeout
        img, uuid = "", ""
        scanned = False
        refreshes = 0
        attempts = 0
        while time.time() < deadline:
            if not uuid:
                if refreshes > 8:
                    Log.warning("二维码多次失效，放弃")
                    return False
                img, uuid = self.create_qr()
                refreshes += 1
                path = save_qr(img, self._qr_path)
                Log.info("扫码登录：二维码已保存 %s", path)
                if on_qr:
                    try:
                        on_qr(path)
                    except Exception:
                        pass
                else:
                    art = qr_ascii(path)
                    if art:
                        print(art)
                    print(f"请用 12306 App 扫码（图片: {path}）")
            time.sleep(1.2)
            code, msg = self.check_qr(uuid)
            # 官方 login_new.js 的 popup_tipsQrInfo 注释就是权威定义：
            #   0 未识别（待扫） 1 已识别暂未授权 2 登录成功（已识别且已授权）
            #   3 已失效         5 系统异常
            if code == "2":
                Log.ok("扫码成功（已授权），正在换取登录票据")
                return True
            if code == "1":
                if not scanned:
                    scanned = True
                    Log.info("已扫码，请在手机上点「确认登录」…")
                continue
            if code == "3":
                if scanned:
                    Log.warning("手机端取消了登录")
                    return False
                attempts += 1
                if attempts > 10:
                    Log.warning("二维码连续失效 %d 次，放弃", attempts)
                    return False
                Log.info("二维码已失效，重新生成")
                uuid = ""
                continue
            if code == "5":
                Log.warning("扫码接口系统异常，稍后重试")
                time.sleep(2.0)
                continue
            # code == "0" 继续等
        Log.warning("扫码超时（%.0f 秒）", timeout)
        return False

    # ------------------------------------------------------------ 验证码 + 密码
    def login_conf(self) -> Dict[str, str]:
        """GET /otn/login/conf → 判断当前是否需要验证码 / 滑块 / UAM 登录。"""
        r = self.s.get(URLS["login_check"], use_cdn=False)
        d = r.get("data")
        if isinstance(d, dict):
            return {k: str(v) for k, v in d.items()}
        return {}

    def captcha_image(self) -> Tuple[str, str]:
        """返回 (base64图, uuid)。answer 是图上的坐标点，如 '40,40,120,40'。

        注：2026-11 实测 is_login_passCode='N'，验证码通道关闭时本接口返回空，
        此时登录表单 answer 传空串即可。
        """
        url = (URLS["captcha_image"]
               + f"&login_site=E&module=login&rand=sjrand&_={int(time.time() * 1000)}")
        r = self.s.get(url, headers=self._passport_headers, use_cdn=False)
        return r.get("image") or "", r.get("uuid") or ""

    def captcha_check(self, answer: str) -> bool:
        r = self.s.get(URLS["captcha_check"] + f"&answer={answer}&_={int(time.time() * 1000)}",
                       headers=self._passport_headers, use_cdn=False)
        return str(r.get("result_code")) == "4"

    def login_by_password(self, answer_provider: Optional[Callable[[str], str]] = None) -> bool:
        self.prepare()
        user, pwd = self.cfg.username, self.cfg.password
        if not user or not pwd:
            Log.warning("账号或密码为空（配置 login.username / login.password）")
            return False
        if not sm4_self_test():
            Log.error("SM4 实现自检失败，无法加密密码")
            return False

        conf = self.login_conf()
        need_code = str(conf.get("is_login_passCode", "Y")).upper() == "Y"
        answer = ""
        if need_code:
            img, _ = self.captcha_image()
            if img:
                path = save_qr(img, os.path.join(paths.RUNTIME, "captcha.png"))
                if answer_provider:
                    answer = answer_provider(path) or ""
                else:
                    print(f"验证码图：{path}")
                    answer = input("请输入验证码点位（多选用逗号，如 40,40,120,40）：").strip()
                if answer and not self.captcha_check(answer):
                    Log.warning("验证码校验未通过，仍尝试提交")
            else:
                Log.info("验证码通道关闭（is_login_passCode=N）")
        else:
            Log.info("当前无需验证码（is_login_passCode=N）")

        data = {
            "loginUserDTO.user_name": user,
            "userDTO.password": password_field(pwd, SM4_KEY),
            "appid": "otn",
            "answer": answer,
        }
        r = self.s.post(URLS["login"], data=data,
                        headers=self._passport_headers, use_cdn=False)
        code = str(r.get("result_code"))
        # 兼容老版：data.loginCheck == 'Y'
        if str(r.get("data.loginCheck", "")).upper() == "Y":
            Log.ok("密码登录成功")
            return True
        msg = r.message or r.text[:160]
        if code != "0":
            Log.warning("密码登录失败(%s): %s", code, msg)
            if ("验证" in msg or "滑块" in msg) and self.cfg.slide in ("browser", "manual"):
                if self.cfg.slide == "browser":
                    Log.info("改用浏览器过滑块…")
                    return self._slide_by_browser(user, pwd)
                Log.info("请手动浏览器登录后，把 cookie 写入 config.yaml 的 login.cookie")
            return False
        Log.ok("密码登录成功")
        return True

    # ------------------------------------------------------------ 浏览器兜底
    def _slide_by_browser(self, user: str, pwd: str) -> bool:
        """用 playwright 打开登录页，人工/自动过滑块，随后取 cookie 回灌。

        未安装 playwright 时静默跳过。
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            Log.info("未安装 playwright，跳过（pip install playwright && playwright install chromium）")
            return False
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=False,
                                      args=["--disable-blink-features=AutomationControlled"])
                ctx = b.new_context(user_agent=self.s.ua, viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                page.goto(UA_LOGIN_REFERER, timeout=45000)
                page.fill("#J-userName", user)
                page.fill("#J-password", pwd)
                page.click("#J-login")
                Log.info("浏览器已打开：请手动完成验证码/滑块（最长 180 秒）")
                for _ in range(180):
                    page.wait_for_timeout(1000)
                    try:
                        if page.locator("#J-login").count() and page.is_visible("#J-login"):
                            continue
                    except Exception:
                        pass
                    if "index.html" in page.url or "leftTicket" in page.url:
                        break
                cookies = ctx.cookies()
                for ck in cookies:
                    if "12306" in ck.get("domain", ""):
                        self.s.http.cookies.set(ck["name"], ck["value"], domain=HOST)
                b.close()
            self.s.save_cookies()
            n = len(self.s.http.cookies)
            Log.ok("浏览器 cookie 已回灌（%d 项）", n)
            return n > 2
        except Exception as e:
            Log.warning("浏览器登录失败: %s", e)
            return False

    # ------------------------------------------------------------ Cookie
    def login_by_cookie(self, cookie: str) -> bool:
        if not cookie:
            return False
        self.s.set_cookie_string(cookie)
        alive = self.check_alive()
        if alive:
            Log.ok("Cookie 登录态有效")
            return True
        Log.warning("Cookie 无效或已过期")
        return False

    # ------------------------------------------------------------ 激活
    def activate(self) -> bool:
        """换 tk → uamauthclient → 进订单页拿 token 与乘客列表。"""
        r = self.s.post(URLS["uamtk"], data={"appid": "otn"},
                        headers=self._passport_headers, use_cdn=False)
        tk = r.get("newapptk") or r.get("uamtk") or r.get("apptk")
        if not tk:
            Log.warning("uamtk 失败: %s", r.text[:160])
            return False
        r2 = self.s.post(URLS["uamauthclient"], data={"tk": tk}, use_cdn=False)
        uname = r2.get("username") or ""
        if str(r2.get("result_code")) not in ("0", "None") and not uname:
            Log.warning("uamauthclient 失败: %s", r2.text[:160])
            return False
        self.s.is_login = True
        self.s.login_at = time.time()
        self.s.username = uname or self.cfg.username
        self.s.save_cookies()
        Log.ok("登录态已激活 user=%s", uname or "(未知)")
        self.refresh_order_context()
        return True

    def refresh_order_context(self) -> bool:
        """进订单页，抓 REPEAT_SUBMIT_TOKEN / passengerDTOs / ticketInfoForPassengerForm。"""
        r = self.s.get(URLS["init_dc"], use_cdn=False)
        if not r.ok or len(r.text) < 500:
            Log.warning("initDc 失败: %s", (r.error or r.text[:100]))
            return False

        def grab(name: str) -> str:
            m = re.search(name + r"\s*=\s*'([^']*)'", r.text)
            if not m:
                m = re.search(name + r'\s*=\s*"([^"]*)"', r.text)
            return m.group(1) if m else ""

        self.s.repeat_submit_token = grab("globalRepeatSubmitToken") or grab("repeat_submit_token")
        info = grab("ticketInfoForPassengerForm")
        if info:
            try:
                self.s.ticket_info = json.loads(info.replace("'", '"'))
            except Exception:
                self.s.ticket_info = {}
        pr = self.s.post(URLS["passengers"], data={"_json_att": ""}, use_cdn=False)
        if pr.ok and isinstance(pr.get("data"), dict):
            self.s.passenger_dtos = pr.get("data.normal_passengers") or []
        Log.info("订单上下文就绪 token=%s 乘客=%d",
                 (self.s.repeat_submit_token or "")[:8], len(self.s.passenger_dtos))
        return bool(self.s.repeat_submit_token)

    def check_alive(self) -> bool:
        """探测登录态。conf 的 is_login 是 'Y'/'N' 字符串，注意别用 bool()。"""
        r = self.s.get(URLS["login_check"], use_cdn=False)
        d = r.get("data")
        if isinstance(d, dict) and "is_login" in d:
            return str(d.get("is_login")).strip().upper() == "Y"
        # 兜底：uamtk 能换到票就说明还有登录态
        r2 = self.s.post(URLS["uamtk"], data={"appid": "otn"},
                         headers=self._passport_headers, use_cdn=False)
        return bool(r2.get("newapptk"))

    # ------------------------------------------------------------ 心跳
    def start_keeper(self, stop_event: Optional[threading.Event] = None,
                     on_relogin: Optional[Callable[[], None]] = None):
        stop = stop_event or self._stop
        interval = max(30, self.cfg.relogin_minutes * 60 / 3)

        def loop():
            while not stop.is_set():
                stop.wait(interval)
                if stop.is_set():
                    break
                age = time.time() - self.s.login_at
                try:
                    alive = self.check_alive()
                except Exception:
                    alive = False
                if not alive or age > self.cfg.relogin_minutes * 60:
                    Log.warning("登录态失效或超时（%.0f 分钟），重新登录", age / 60)
                    try:
                        if self.ensure():
                            if on_relogin:
                                on_relogin()
                    except Exception as e:
                        Log.warning("重登异常: %s", e)

        t = threading.Thread(target=loop, name=f"keeper-{self.s.name}", daemon=True)
        t.start()
        return t


def prepare_session(sess: Session, on_qr: Optional[Callable[[str], None]] = None) -> bool:
    """一站式：预热 → 登录 → 激活 → 订单上下文。"""
    lm = LoginManager(sess)
    if not lm.ensure(on_qr=on_qr):
        return False
    if not sess.repeat_submit_token:
        lm.refresh_order_context()
    return True
