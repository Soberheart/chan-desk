"""国内行情接口（不走东方财富、不走 baostock）。

在海外网络、或东方财富 / baostock 连不上时用。接口选型参考 simonlin1212/a-stock-data（Apache-2.0）的实测；
通达信盘后包的二进制布局参考 jing2uo/tdx2db（MIT）。

  腾讯 K 线        不复权日线（含换手率、成交额），指数日线，最近 320 根 5 / 30 分钟线。三个入口轮换，单入口约 600 次后限流
  新浪复权因子     等比前复权（腾讯自带的前复权是“等差”口径，早年价格会失真甚至变负）
  新浪股票列表     全部沪深 A 股代码和名称（名称带 ST 即 ST）
  新浪概念板块     700 多个“热门概念”及成分股，当作题材
  通达信盘后包     某交易日全市场日线（含昨收、成交额），每天收盘后几小时发布，日常增量更新用
  同花顺涨停揭秘   涨停原因、首封时间、开板次数、板型（只有最近一两个月）
"""
import io
import json
import math
import re
import struct
import threading
import time
import zipfile
from datetime import date, datetime, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_local = threading.local()


def _session():
    import requests
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = UA
        _local.s = s
    return s


def http_get(url, params=None, headers=None, timeout=(8, 25), retries=3, ok404=False):
    last = None
    for i in range(retries):
        try:
            r = _session().get(url, params=params, headers=headers or {}, timeout=timeout)
            if ok404 and r.status_code == 404:
                return r
            r.raise_for_status()
            return r
        except Exception as e:  # noqa
            last = e
            time.sleep(1 + 2 * i)
    raise RuntimeError(f"请求失败 {url}: {type(last).__name__}: {str(last)[:150]}")


def tc_symbol(code):
    return code.replace(".", "")          # sh.600000 → sh600000


# ---------------------------------------------------------------- 腾讯 K 线
TC_HOSTS = ["https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg", "https://ifzq.gtimg.cn"]
_down_until = {}


def _tc_call(path, param):
    errs = []
    for _ in range(2):
        for host in TC_HOSTS:
            if _down_until.get(host, 0) > time.time():
                continue
            try:
                r = http_get(host + path, params={"param": param}, headers={"Referer": "https://gu.qq.com/"},
                             retries=1)
                j = r.json() if r.text.strip() else {}
            except Exception as e:  # noqa
                errs.append(f"{host}: {e}")
                _down_until[host] = time.time() + 120
                continue
            if isinstance(j, dict) and j.get("msg") == "param error":
                raise ValueError(f"腾讯 K 线参数错误: {param}")
            if isinstance(j, dict) and isinstance(j.get("data"), dict) and j["data"]:
                return j["data"]
            errs.append(f"{host}: 空响应（可能被限流）")
            _down_until[host] = time.time() + 120
        if all(_down_until.get(h, 0) > time.time() for h in TC_HOSTS):
            time.sleep(max(1, min(_down_until.values()) - time.time()))
    raise RuntimeError("腾讯 K 线三个入口都不可用：" + "；".join(errs[-3:]))


def tc_daily_raw(code, start, end):
    """不复权日线，按日期升序。返回 (rows, name)。rows: dict(date, open, high, low, close, volume[股], amount[元], turn[%])。"""
    sym = tc_symbol(code)
    shares_unit = 1 if sym[2:5] in ("688", "689") else 100      # 科创板成交量单位是股，其余是手
    out, name, e = {}, None, end
    for _ in range(30):
        data = _tc_call("/appstock/app/newfqkline/get", f"{sym},day,{start},{e},640,")
        d = data.get(sym) or {}
        if name is None:
            qt = (d.get("qt") or {}).get(sym)
            if isinstance(qt, list) and len(qt) > 1:
                name = qt[1]
        part = d.get("day") or d.get("qfqday") or []
        for r in part:
            if not (start <= r[0] <= end) or r[0] in out:
                continue
            try:
                o, c, h, lo, v = (float(x) for x in r[1:6])
            except (TypeError, ValueError):
                continue
            turn = _num(r[7]) if len(r) > 7 else None
            amt = _num(r[8]) if len(r) > 8 else None
            out[r[0]] = {"date": r[0], "open": o, "high": h, "low": lo, "close": c, "volume": v * shares_unit,
                         "amount": amt * 1e4 if amt is not None else None, "turn": turn}
        if len(part) < 600 or not part or part[0][0] <= start:
            break
        e = (date.fromisoformat(part[0][0]) - timedelta(days=1)).isoformat()
    return [out[k] for k in sorted(out)], name


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def tc_minutes(code, period="m5"):
    """最近 320 根分钟线（不复权）。返回 [(YYYY-MM-DD, HH:MM, open, high, low, close, volume)]，按时间升序。"""
    sym = tc_symbol(code)
    data = _tc_call("/appstock/app/kline/mkline", f"{sym},{period},,320")
    rows = (data.get(sym) or {}).get(period) or []
    out = []
    for r in rows:
        t = str(r[0])
        try:
            o, c, h, lo, v = (float(x) for x in r[1:6])
        except (TypeError, ValueError):
            continue
        out.append((f"{t[:4]}-{t[4:6]}-{t[6:8]}", f"{t[8:10]}:{t[10:12]}", o, h, lo, c, v))
    return out


# ---------------------------------------------------------------- 新浪复权因子 + 列表 + 概念
def sina_factors(code):
    """前复权因子阶梯，按日期升序 [(生效日, f)]；前复权价 = 不复权价 / f。"""
    sym = tc_symbol(code)
    r = http_get(f"https://finance.sina.com.cn/realstock/company/{sym}/qfq.js",
                 headers={"Referer": "https://finance.sina.com.cn/"})
    text = r.text
    i = text.find("{")
    if i < 0:
        return []
    data, _ = json.JSONDecoder().raw_decode(text[i:])
    out = []
    for it in data.get("data") or []:
        try:
            out.append((it["d"], float(it["f"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def factor_on(factors, d):
    """取不晚于 d 的最近一个因子；比所有生效日都早时用最早的那个。"""
    f = factors[0][1] if factors else 1.0
    for fd, fv in factors:
        if fd <= d:
            f = fv
        else:
            break
    return f


SINA_API = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"


def _sina_json(r):
    t = r.content.decode("gbk", "ignore").strip()
    return json.loads(t) if t and t != "null" else []


def sina_stock_list():
    """全部沪深 A 股 [(code, name)]，code 形如 sh.600000。"""
    out, page = [], 1
    while page < 200:
        r = http_get(f"{SINA_API}/Market_Center.getHQNodeData",
                     params={"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a"},
                     headers={"Referer": "https://finance.sina.com.cn/"})
        rows = _sina_json(r)
        if not rows:
            break
        for x in rows:
            sym = x.get("symbol", "")
            if sym[:2] in ("sh", "sz") and len(sym) == 8:
                out.append((f"{sym[:2]}.{sym[2:]}", x.get("name", "")))
        page += 1
    return out


def sina_concepts():
    """新浪“热门概念”板块 [(节点, 名称)]。"""
    r = http_get(f"{SINA_API}/Market_Center.getHQNodes", headers={"Referer": "https://finance.sina.com.cn/"})
    j = _sina_json(r)
    found = {}

    def walk(x):
        if isinstance(x, list):
            if len(x) >= 3 and isinstance(x[0], str) and isinstance(x[2], str) and x[2].startswith("chgn_"):
                found[x[2]] = x[0]
            for y in x:
                walk(y)
    walk(j)
    return sorted(found.items())


def sina_concept_members(node, max_members=300):
    """概念成分 [code]；成分超过 max_members 返回 None（太宽泛，不当题材）。"""
    out = []
    for page in range(1, max_members // 100 + 2):
        r = http_get(f"{SINA_API}/Market_Center.getHQNodeData",
                     params={"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": node},
                     headers={"Referer": "https://finance.sina.com.cn/"})
        rows = _sina_json(r)
        if not rows:
            break
        for x in rows:
            sym = x.get("symbol", "")
            if sym[:2] in ("sh", "sz") and len(sym) == 8:
                out.append(f"{sym[:2]}.{sym[2:]}")
        if len(rows) < 100:
            break
    else:
        return None
    return out if len(out) <= max_members else None


# ---------------------------------------------------------------- 通达信盘后包
TDX_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"


def tdx_package(day):
    """某交易日沪深全市场日线 {sh.600000: dict(prev_close, open, high, low, close, volume[股], amount[元], name)}。
    当天包还没发布或非交易日返回 None。"""
    ymd = day.replace("-", "")
    r = http_get(TDX_URL.format(ymd=ymd), timeout=(10, 90), ok404=True)
    if r.status_code == 404 or not r.content.startswith(b"PK"):
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    out = {}
    for mk in ("sh", "sz"):
        cn, mn = f"{mk}{ymd[2:]}.cod", f"{mk}{ymd[2:]}.md1"
        if cn not in names or mn not in names:
            raise RuntimeError(f"通达信盘后包缺少 {cn}/{mn}，格式可能变了")
        cod, md1 = z.read(cn), z.read(mn)
        if len(cod) % 150 or len(md1) % 512:
            raise RuntimeError("通达信盘后包长度不对，可能下载不完整")
        for off in range(0, len(cod), 150):
            rec = cod[off:off + 150]
            code = rec[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
            if not re.fullmatch(r"[0-9]{6}", code):
                continue
            seq = struct.unpack("<H", rec[32:34])[0]
            blk = md1[seq * 512:(seq + 1) * 512]
            if len(blk) != 512:
                continue
            pc = struct.unpack("<d", blk[4:12])[0]
            o, h, lo, c = struct.unpack("<4d", blk[12:44])
            vol = struct.unpack("<Q", blk[56:64])[0]
            amt = struct.unpack("<d", blk[72:80])[0]
            if not all(math.isfinite(v) for v in (pc, o, h, lo, c, amt)) or c <= 0:
                continue
            try:
                name = rec[40:72].split(b"\x00")[0].decode("gbk").strip()
            except UnicodeDecodeError:
                name = ""
            out[f"{mk}.{code}"] = {"prev_close": pc, "open": o, "high": h, "low": lo, "close": c,
                                   "volume": float(vol), "amount": amt, "name": name}
    if len(out) < 5000:
        raise RuntimeError(f"通达信盘后包只有 {len(out)} 条记录，可能残缺")
    return out


# ---------------------------------------------------------------- 同花顺涨停揭秘
THS_FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"


def ths_limit_up(day):
    """某日涨停池（只有最近一两个月）[dict(code, reason, board_type, high_days, first_time, open_num)]。"""
    out, page = [], 1
    while page < 10:
        r = http_get("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool",
                     params={"page": page, "limit": 200, "field": THS_FIELDS, "filter": "HS,GEM2STAR",
                             "order_field": "330324", "order_type": "0", "date": day.replace("-", "")},
                     retries=2)
        data = (r.json() or {}).get("data") or {}
        info = data.get("info") or []
        for it in info:
            c = str(it.get("code") or "")
            if len(c) != 6:
                continue
            ft = it.get("first_limit_up_time")
            lt = it.get("last_limit_up_time")
            out.append({"code": ("sh." if c[0] == "6" else "sz.") + c, "reason": it.get("reason_type") or "",
                        "board_type": it.get("limit_up_type") or "", "high_days": it.get("high_days") or "",
                        "first_time": datetime.fromtimestamp(int(ft)).strftime("%H:%M") if ft else None,
                        "last_time": datetime.fromtimestamp(int(lt)).strftime("%H:%M") if lt else None,
                        "open_num": int(it.get("open_num") or 0)})
        total = (data.get("page") or {}).get("total") or 0
        if len(out) >= total or not info:
            break
        page += 1
        time.sleep(0.5)
    return out
