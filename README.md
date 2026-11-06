# TicketSniper · 12306 抢票引擎

> 在 py12306 / testerSunshine 之上重写的抢票内核：**围绕车站起售时刻打窗口**，而不是匀速轮询。
> 纯 Python，无浏览器依赖（扫码登录可全流程无头），带 Web 控制台与实时日志。

```
  ______ _ _        _        _____       _
 |__   _(_) | ___  | |      / ____|     (_)
    | | | | |/ _ \ | |     | (___  _ __  _ _ __   ___ _ __
    | | | | |  __/ | |      \___ \| '_ \| | '_ \ / _ \ '__|
    | | |_| |_|\___| |_     ____) | | | | | |_) |  __/ |
    |_|  |_|\___/|_|_|    |_____/|_| |_|_| .__/ \___|_|
                                          | |
       12306 抢票引擎 v1.0                 |_|
```

---

## 一、和现成项目什么关系

调研了 GitHub 上全部主流 12306 项目，选了两个最有价值的做参考基准，**读源码吸收协议，不 fork、不套壳、不装依赖**。

| 项目 | Star | 状态 | 我借鉴了什么 | 我没照抄的 |
|---|---|---|---|---|
| [testerSunshine/12306](https://github.com/testerSunshine/12306) | 34k | 已归档（2023） | 接口清单、余票字段下标、验证码识别思路 | 协议已过期（无候补、无选座、SM4 之前的老加密）、依赖重 |
| [pjialin/py12306](https://github.com/pjialin/py12306) | 15k | 维护中 | 下单七步链路、`passengerTicketStr` 拼装格式、`getQueueCount` 的车次日期格式、CDN 探针思路 | `pyppeteer` + `requests-html`：每个进程拉一个 Chromium、无连接池、无全局限速、无窗口调度 |
| TicketSniper（本项目） | — | — | httpx HTTP/2 连接池 · 令牌桶限速 · CDN 优选 · **起售时刻窗口调度** · SM4 无头密码登录 · SSE 实时日志 · SQLite 账本 | — |

### 我做的优化（都是老项目没有的）

1. **起售时刻表 + 放票窗口调度**（`sale.py`）
   12306 每天在各站固定时刻放出「第 15 天」的票，抢票胜负 90% 决定于放票前后 3 秒。
   老项目全是恒定 1s 轮询，白白在无票时段烧请求、到点又踩不到节奏。
   本项目从官方接口 `queryAllCacheSaleTime` 拉全量 3181 个车站的起售时刻，反推出任意乘车日的**精确放票时间**，然后三态调度：
   `idle`（远，30s 醒一次）→ `warmup`（放票前 2 分钟，降频预热）→ `burst`（放票前 3s 到放票后 60s，间隔 0.25s、限速拉到 12 QPS）。

2. **纯 Python SM4**（`crypto_sm4.py`，190 行，零依赖）
   12306 的密码登录用国密 SM4-ECB 加密密码。老项目引 `gmssl` 或干脆只支持扫码。
   本项目手写 SM4，通过国标测试向量 + 官方 JS 向量双向校验，密码登录不需要浏览器。

3. **无头扫码登录**（`login.py`）
   逆向 `login_new_v20260207.js` 拿到关键常量后，`create-qr64` / `checkqr` 全走 POST + `appid=otn`，**全程不需要 playwright**。
   拿到二维码 base64 → PIL 转终端 ASCII 二维码（终端也能扫）→ 轮询状态 → 回灌 cookie → `uamtk` + `uamauthclient` 换票。

4. **余票路由自适应**（`query.py`）
   12306 的 `queryZ/queryA/queryG/queryO` 四个路由是**动态漂移**的，请求错了会返回 `302 + {"c_url":"leftTicket/queryG"}`。
   老项目写死路由，路由一变就全挂。本项目读 `c_url` 自动重排路由优先级，且跨进程缓存。

5. **令牌桶 + 抖动 + 退避**
   全局限速（默认 4 QPS，突发期自动上调），请求间隔带 ±35% 抖动，5xx 换 CDN 节点重试，风控类错误指数退避 —— 而不是老项目那种「睡固定秒数」。

6. **选座偏好**（`order.py::resolve_choose_seats`）
   `靠窗` / `过道` / `F` / `A` 等语义自动翻译成 12306 的 `choose_seats` 参数，多人同行按顺序分配；普速车自动跳过（官方不支持）。

7. **Web 控制台**（`web/index.html` + `web/app.py`，纯静态单页，无需 npm 构建）
   任务卡 / 余票预览 / 起售倒计时 / SSE 实时日志 / 扫码登录 / CDN 节点池 / 订单账本。

---

## 二、安装

```bash
# 方式一：作为可安装包（会生成 ticketsniper 命令）
pip install -e .

# 方式二：只装依赖，用 python -m ticketsniper 调用
pip install -r requirements.txt
```

Python ≥ 3.9。核心依赖只有 `httpx[http2]`、`h2`、`PyYAML`、`fastapi`、`uvicorn`、`pillow`。
**不需要浏览器**（`playwright` 只在密码登录撞到滑块时才用，默认关闭）。

## 三、五分钟上手

```bash
# 1. 生成配置
python -m ticketsniper init

# 2. 查一下起售时刻（重要！决定什么时候开抢）
python -m ticketsniper sale 北京南 --date 2026-11-21

# 3. 扫码登录（终端直接打印二维码，手机 12306 APP 扫）
python -m ticketsniper login

# 4. 看看账号下有哪些乘车人
python -m ticketsniper passengers

# 5. 改 tasks.yaml 填车次/席别/乘客，然后开抢
python -m ticketsniper run
```

### 先体检

```bash
python -m ticketsniper doctor        # 开抢前一键体检
```

它会依次检查：配置与任务（站点解析、日期是否在 15 天预售期内、乘客姓名）、
网络延迟、四个余票路由哪个还活着、起售时间表与距放票时间、当前登录态与乘车人数量、
CDN 池、通知渠道、历史成功率。结论分三档：**可以开抢 / 基本可用 / 有硬伤**。

好处是问题在开抢前暴露，而不是等到放票那一刻才发现 cookie 过期或站名写错。

## 先演练，别一上来就真下单

```bash
python -m ticketsniper run --dry      # 只查询、命中就打印，不提交订单
python -m ticketsniper run --once     # 跑一轮就退出
```

演练模式**不需要登录**，可以先验证站点解析、时间窗过滤、席别优先级对不对。

## 四、配置

### `config.yaml`

```yaml
net:
  rate_per_sec: 4.0        # 全局 QPS，突发期自动上调到 burst_rate
  query_workers: 3         # 并发查询任务数
  query_interval: 0.8      # 单任务查询间隔（秒）
  jitter: 0.35             # 间隔抖动比例，别让请求太有规律
  use_cdn: true            # 走 CDN 优选 IP
  proxy: ""                # 需要时填 http://user:pass@host:port

login:
  mode: qrcode             # qrcode | password | cookie
  relogin_minutes: 90      # 登录态超过这个时间自动重登

notify:
  channels: [console, bark]   # console/serverchan/bark/telegram/webhook/email
  bark_url: ""
  serverchan_key: ""
  sound: true

web:
  port: 8848
  token: ""                # 填了就要求 ?token= 访问
```

密码不想写进文件就 `export TS_PASSWORD=xxx`，cookie 同理 `export TS_COOKIE=xxx`。

### `tasks.yaml`

```yaml
tasks:
  - id: T01
    name: 北京南→上海虹桥 早班
    enabled: true
    from_station: 北京南        # 站名/电报码/拼音都行
    to_station: 上海虹桥
    dates: ["+1"]              # "+1" = 明天；也可 "2026-11-21" 或 "+3,+10" 区间
    trains: []                 # 空=不限；["G1","G3"] 白名单
    exclude_trains: ["G547"]   # 黑名单
    seats: [二等座, 一等座]      # 顺序即优先级
    depart_between: "06:00-12:00"
    arrive_between: ""
    exact_station: false       # true = 只买站名完全一致（防买到中途站）
    passengers:
      - name: 张三             # 只写名字，从账号乘车人里按名字匹配
      # - name: 李四
      #   id_no: 110101199001011234   # 也可直接写证件号
    seat_prefer: [靠窗]        # 选座偏好，仅动车组生效
    max_attempts: 0            # 0=不限
    allow_upgrade: false       # true = 二等抢不到自动试一等
    allow_no_seat: false
    stop_on_success: true      # 抢到就停所有任务
```

## 五、Web 控制台

```bash
python -m ticketsniper web          # http://127.0.0.1:8848
```

- 顶部状态条：运行中 / QPS / CDN 节点数 / 登录态
- 任务卡：每条任务的轮次、命中次数、最近车次与席别
- 余票预览：填出发/到达/日期直接查，**同时显示该站起售时刻和倒计时**
- 实时日志：SSE 推送，命中行紫色、成功行绿色
- 扫码登录：点一下就出二维码图，手机扫完自动回灌 cookie（Web 与命令行共用 `runtime/sessions/`）

## 六、命令行一览

| 命令 | 作用 |
|---|---|
| `init` | 生成 `config.yaml` / `tasks.yaml` |
| `sale <站> [--date] [--table]` | 起售时刻、下次放票倒计时、某日票何时放 |
| `login [--mode qrcode\|password\|cookie]` | 登录并存 cookie |
| `passengers` | 列出账号下乘车人（填 tasks.yaml 用） |
| `query [出发] [到达] [日期] [--seat] [--depart]` | 查余票 |
| `run [-t T01] [--dry] [--once] [--rate N]` | 开抢 |
| `cdn [--limit N]` | 探测 CDN 优选 IP |
| `web [--port N]` | 起控制台 |
| `doctor [-t T01]` | 开抢前体检（网络/路由/起售/登录/通知） |
| `status` | 任务与订单账本 |

## 七、项目结构

```
ticketsniper/
├── api/
│   ├── constants.py    接口 URL、席别映射、字段下标、错误分类
│   └── station.py      车站库（3181 站，官方 JS 解析 + 30 天缓存）
├── crypto_sm4.py       SM4-ECB（纯 Python，国标向量自校验）
├── login.py            扫码 / 密码 / cookie 登录，uamtk 换票，订单上下文预热
├── net.py              httpx 连接池 + 令牌桶 + CDN 优选 + 响应分类
├── query.py            余票查询（路由自适应）、车次解析、候选打分
├── sale.py             起售时刻表 + 放票窗口三态调度
├── order.py            下单七步链路、票串拼装、选座偏好
├── worker.py           任务线程、多 session 竞速去重、状态机
├── notify.py           6 个推送渠道 + 提示音
├── store.py            SQLite 订单账本 + JSONL 流水
├── doctor.py           开抢前体检（网络/路由/起售/登录/通知一屏红绿灯）
├── web/app.py          FastAPI 后端（SSE 日志流、任务 CRUD、起售倒计时）
├── web/index.html      单页控制台（改完刷新即可，后端按 mtime 热加载）
├── cli.py              命令行
└── selftest.py         49 项离线自测（mock 整个 12306）
```

## 八、下单链路（实测协议）

```
submitOrderRequest    secretStr(URL 解码) + train_date + tour_flag=dc + purpose_codes=ADULT
        ↓
initDc                取 globalRepeatSubmitToken + ticketInfoForPassengerForm
        ↓
checkOrderInfo        passengerTicketStr / oldPassengerStr / bed_level_order_num=30个0
        ↓
getQueueCount         查排队人数，op_2=='true' 直接判无票
        ↓
confirmSingleForQueue seatDetailType=000 + choose_seats
        ↓
queryOrderWaitTime    轮询 orderId 非 '0' 即出票
        ↓
resultOrderForDcQueue 落库
```

票串格式（第 8 段是座位字母，这是选座生效的地方）：

```
passengerTicketStr = "O,0,1,张三,1,110101199001011234,13800138000,N,F"
oldPassengerStr    = "张三,1,110101199001011234,1_"
```

## 九、自测

```bash
python -m ticketsniper.selftest
```

启动一个 mock 12306 服务器，把 `URLS` 全量重定向过去，跑 **49 项断言**：
SM4 三组测试向量、票串拼装、59 段余票串解析、下单七步全链路（含 6 个接口调用核对与参数格式断言）、`c_url` 路由切换、起售窗口三态、选座偏好翻译、体检报告聚合、乘客解析与通知文案。

实测输出：`全部通过（49 项）`

## 十、实测记录（2026-11-07 线上）

| 项目 | 结果 |
|---|---|
| 余票查询 | 北京南→上海虹桥 2026-11-09，55 趟，约 3.5s |
| 预售期 | 15 天（含当天），`today+14` 内有数据，`+15` 起返回 HTML |
| 扫码登录 | `create-qr64` 返回 199×199 真实可扫二维码，`checkqr` 状态机正常 |
| 起售时刻表 | 3181 站，北京南 12:45 / 上海虹桥 13:45 |
| CDN 优选 | 5 个 edge 节点，RTT 69–167ms |
| 演练下单 | 命中 G547 二等座 → 候选 25 个 → 正常退出 |

## 十一、已知限制

- **未做过真实下单的端到端验证**：手上没有可用的 12306 账号，下单链路是靠 mock 服务器逐参数核对协议正确性，而不是真出了一张票。首次真实使用时请先 `--dry` 演练一轮，再盯一眼日志里的 `排队: 前 N 人 / 余票 M`。
- **候补下单未实现**：`/otn/afterNate/submitOrder` 等接口需登录且行为复杂，py12306 也没做。`allow_waitlist` 字段保留，当前行为是「没票就继续轮询」。
- **验证码通道当前关闭**：`/otn/login/conf` 返回 `is_login_passCode='N'`，密码登录 `answer` 传空即可。哪天官方重新打开验证码，`captcha_image()` / `captcha_check()` 已经备好，只是没有接 OCR。
- **滑块**：密码登录触发风控滑块时，`login.slide: browser` 会用 playwright 打开有头浏览器过滑块（需 `pip install playwright && playwright install chromium`）；扫码登录完全绕开这一步。
- **CDN 只做探针与降级**：实测通过 edge IP 请求 `/otn/leftTicket/queryZ` 会返回 200 但 body 是 1904 字节的 HTML 首页片段（`<!DOCTYPE html ...`），直连才是 JSON。所以余票与下单请求一律直连，CDN 池只用于可用性探测和 5xx 时的兜底重试。
- **改签/退票**：不在范围内。

## 十二、调参建议（实战）

| 场景 | 参数 |
|---|---|
| 平时挂着等退票 | `rate_per_sec: 2`、`query_interval: 2.0`，一晚上也就几万请求 |
| 卡起售点抢热门车次 | 保持默认 `4`，`burst_rate: 12` 只在放票前后 60s 生效 |
| 多任务多日期 | `query_workers` 别超过 `rate_per_sec`，否则全在令牌桶里排队 |
| 老是被风控 | 降 `rate_per_sec` 到 1.5，把 `jitter` 提到 0.5；CDN 只影响探针，不改变查询出口 |

`--dry` 演练 + 日志里的 `排队: 前 N 人 / 余票 M` 是判断「到底抢不抢得到」的唯一依据：
`countT` 一直很大说明这条线根本没人退票，换车次比加 QPS 有用。
