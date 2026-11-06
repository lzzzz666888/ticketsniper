# -*- coding: utf-8 -*-
"""车站数据：站名 → 电报码，拼音/首字母反查。

优先用本地缓存 stations.json；首次运行时从 12306 station_name.js 拉取并落盘，
拉取失败则退化到内置的常用站表（仍可跑，只是覆盖不全）。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from ..config import paths

_STATION_JS = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"

# 兜底表：一线/热门城市，防止首次联网失败直接不可用
_FALLBACK = """
北京|BJP|beijing|bj 北京南|VNP|beijingnan|bjn 北京西|BXP|beijingxi|bjx 北京北|VAP|beijingbei|bjb
上海|SHH|shanghai|sh 上海虹桥|AOH|shanghaihongqiao|shhq 上海南|SNH|shanghainan|shn
广州|GZQ|guangzhou|gz 广州南|IZQ|guangzhounan|gzn 广州东|GGQ|guangzhoudong|gzd
深圳|SZQ|shenzhen|sz 深圳北|IOQ|shenzhenbei|szb 杭州|HZH|hangzhou|hz 杭州东|HGH|hangzhoudong|hzd
南京|NJH|nanjing|nj 南京南|NKH|nanjingnan|njn 天津|TJP|tianjin|tj 天津西|TXP|tianjinxi|tjx
武汉|WHN|wuhan|wh 汉口|HKN|hankou|hk 武昌|WCN|wuchang|wc 成都|CDW|chengdu|cd 成都东|ICW|chengdudong|cdd
重庆|CQW|chongqing|cq 重庆北|CUW|chongqingbei|cqb 西安|XAY|xian|xa 西安北|EAY|xianbei|xab
长沙|CSQ|changsha|cs 长沙南|CWQ|changshanan|csn 郑州|ZZF|zhengzhou|zz 郑州东|ZAF|zhengzhoudong|zzd
济南|JNK|jinan|jn 济南西|JGK|jinanxi|jnx 青岛|QDK|qingdao|qd 青岛北|QHK|qingdaobei|qdb
沈阳|SYT|shenyang|sy 沈阳北|SBT|shenyangbei|syb 大连|DLT|dalian|dl 大连北|DFT|dalianbei|dlb
哈尔滨|HBB|haerbin|heb 哈尔滨西|HXB|haerbinxi|hebx 长春|CCT|changchun|cc
福州|FZS|fuzhou|fz 福州南|FYS|fuzhounan|fzn 厦门|XMS|xiamen|xm 厦门北|XKS|xiamenbei|xmb
南昌|NCG|nanchang|nc 南昌西|NXG|nanchangxi|ncx 合肥|HFH|hefei|hf 合肥南|ENH|hefeinan|hfn
昆明|KMM|kunming|km 昆明南|KOM|kunmingnan|kmn 贵阳|GIW|guiyang|gy 贵阳北|KQW|guiyangbei|gyb
南宁|NNZ|nanning|nn 南宁东|NFZ|nanningdong|nnd 海口|VUQ|haikou|hk 海口东|KEQ|haikoudong|hkd
三亚|SEQ|sanya|sy 兰州|LZJ|lanzhou|lz 兰州西|LAJ|lanzhouxi|lzx 西宁|XNO|xining|xn
银川|YIJ|yinchuan|yc 呼和浩特|HHC|huhehaote|hhht 乌鲁木齐|WAR|wulumuqi|wlmq
太原|TYV|taiyuan|ty 太原南|TNV|taiyuannan|tyn 石家庄|SJP|shijiazhuang|sjz 石家庄北|SVP|shijiazhuangbei|sjzb
苏州|SZH|suzhou|sz 苏州北|OHH|suzhoubei|szb 无锡|WGH|wuxi|wx 无锡东|WGH|wuxidong|wxd
宁波|NGH|ningbo|nb 温州|RZH|wenzhou|wz 温州南|VRH|wenzhounan|wzn
东莞|RTQ|dongguan|dg 东莞东|DMQ|dongguandong|dgd 佛山|FSQ|foshan|fs 珠海|ZHQ|zhuhai|zh
香港西九龙|XJA|xianggangxijiulong|xgxjl
"""


def _parse_js(text: str) -> List[Tuple[str, str, str, str]]:
    """station_name.js → [(中文名, 电报码, 拼音, 简拼), ...]"""
    out: List[Tuple[str, str, str, str]] = []
    for chunk in re.findall(r"@[^@]+", text):
        parts = chunk.lstrip("@").split("|")
        if len(parts) >= 8:
            out.append((parts[1], parts[2], parts[3], parts[4]))
    return out


def _parse_fallback() -> List[Tuple[str, str, str, str]]:
    items = []
    for line in _FALLBACK.strip().splitlines():
        for token in re.split(r"\s{2,}|\s(?=[^\s]+\|[A-Z]{3}\|)", line.strip()):
            parts = token.split("|")
            if len(parts) == 4:
                items.append((parts[0], parts[1], parts[2], parts[3]))
    return items


class StationBook:
    """站名解析器。线程安全只要不写缓存即可。"""

    def __init__(self, cache_ttl: int = 30 * 86400):
        self.ttl = cache_ttl
        self.by_name: Dict[str, Tuple[str, str, str, str]] = {}
        self.by_code: Dict[str, Tuple[str, str, str, str]] = {}
        self.items: List[Tuple[str, str, str, str]] = []
        self.loaded_at = 0.0
        self._load()

    # ---------------- 载入
    def _load(self):
        f = paths.STATION_CACHE
        if os.path.exists(f) and time.time() - os.path.getmtime(f) < self.ttl:
            try:
                data = json.load(open(f, encoding="utf-8"))
                self.items = [tuple(x) for x in data["items"]]
                self._index()
                self.loaded_at = os.path.getmtime(f)
                return
            except Exception:
                pass
        items = self._fetch_remote() or _parse_fallback()
        self.items = items
        self._index()
        try:
            json.dump({"items": items, "fetched": int(time.time())},
                      open(f, "w", encoding="utf-8"), ensure_ascii=False)
            self.loaded_at = time.time()
        except Exception:
            pass

    def _fetch_remote(self) -> Optional[List[Tuple[str, str, str, str]]]:
        try:
            import httpx
            r = httpx.get(_STATION_JS, timeout=10, verify=False,
                          headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and "station_version" in r.text:
                items = _parse_js(r.text)
                if len(items) > 1000:
                    return items
        except Exception:
            return None
        return None

    def _index(self):
        self.by_name = {i[0]: i for i in self.items}
        self.by_code = {i[1]: i for i in self.items}

    def refresh(self):
        self._load()

    # ---------------- 查询
    def resolve(self, text: str) -> Optional[Tuple[str, str, str, str]]:
        """接受 中文名 / 电报码 / 全拼 / 简拼，返回 (名, 码, 拼音, 简拼)。"""
        if not text:
            return None
        t = str(text).strip()
        if t in self.by_name:
            return self.by_name[t]
        if t.upper() in self.by_code:
            return self.by_code[t.upper()]
        low = t.lower()
        exact = [i for i in self.items if i[2] == low or i[3] == low]
        if exact:
            return exact[0]
        fuzzy = [i for i in self.items if i[2].startswith(low) or i[3].startswith(low)]
        if len(fuzzy) == 1:
            return fuzzy[0]
        return None

    def code(self, text: str) -> Optional[str]:
        hit = self.resolve(text)
        return hit[1] if hit else None

    def name(self, code: str) -> str:
        hit = self.by_code.get(str(code).upper())
        return hit[0] if hit else str(code)

    def suggest(self, text: str, limit: int = 8) -> List[Tuple[str, str]]:
        low = str(text).strip().lower()
        if not low:
            return []
        res = [i for i in self.items
               if low in i[0].lower() or i[2].startswith(low) or i[3].startswith(low)]
        return [(i[0], i[1]) for i in res[:limit]]


book = StationBook()
