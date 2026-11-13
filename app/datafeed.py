"""行情下载：baostock（真实）或合成数据（MOCK 模式，离线测试用）。

日线使用前复权（adjustflag=2）。增量更新时，如果最后一根已存K线的收盘价和新拉到的不一致，
说明发生了除权，会自动重新下载该股票全部历史。
"""
import datetime as dt
import hashlib
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache

import numpy as np
import pandas as pd

from . import db
from .settings import BENCHMARK, DATA_SOURCE, DATA_START, DOWNLOAD_WORKERS, MARKET_INDEXES, MOCK

BOARD_PREFIX = ("sh.60", "sh.68", "sz.00", "sz.30")
DAY_FIELDS = "date,open,high,low,close,volume,amount,turn,tradestatus,isST,pctChg"


def today():
    return dt.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- baostock
BS_TIMEOUT = 30   # 秒。baostock 用裸 socket、默认不超时，网络一卡就会永远挂住整个下载任务


class _BS:
    """每个进程各自登录一次 baostock。"""
    logged = False

    @classmethod
    def ensure(cls):
        import socket

        import baostock as bs
        if not cls.logged:
            socket.setdefaulttimeout(BS_TIMEOUT)
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
            cls.logged = True
        return bs


def _rs_to_df(rs):
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def bs_stock_list():
    bs = _BS.ensure()
    df = _rs_to_df(bs.query_stock_basic())
    df = df[(df["type"] == "1") & (df["status"] == "1")]
    df = df[df["code"].str.startswith(BOARD_PREFIX)]
    return [(r.code, r.code_name, r.ipoDate) for r in df.itertuples()]


def bs_daily(code, start, end, index=False):
    bs = _BS.ensure()
    fields = "date,open,high,low,close" if index else DAY_FIELDS
    err = ""
    for attempt in range(3):
        try:
            rs = bs.query_history_k_data_plus(code, fields, start_date=start, end_date=end,
                                              frequency="d", adjustflag="3" if index else "2")
            if rs.error_code == "0":
                df = _rs_to_df(rs)
                break
            err = rs.error_msg
        except OSError as e:          # 超时 / 连接被断开 → 重新登录再试
            err = f"网络超时或断开（{e}）"
        _BS.logged = False
        bs = _BS.ensure()
    else:
        raise RuntimeError(f"{code}: {err}")
    if df.empty:
        return df
    num = [c for c in df.columns if c not in ("date", "tradestatus", "isST")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


def bs_30m(code, start, end):
    bs = _BS.ensure()
    rs = bs.query_history_k_data_plus(code, "time,open,high,low,close,volume", start_date=start,
                                      end_date=end, frequency="30", adjustflag="2")
    df = _rs_to_df(rs)
    if df.empty:
        return df
    t = df["time"].str
    df["time"] = t[0:4] + "-" + t[4:6] + "-" + t[6:8] + " " + t[8:10] + ":" + t[10:12]
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ---------------------------------------------------------------- mock
# 合成行情：所有股票共享一个“情绪周期”（低位 → 主升 → 退潮），每轮有两个活跃题材，
# 活跃题材里的股票更容易涨停、连板；退潮时被炒作过的股票容易跌停。只用于离线测试。
MOCK_N = int(os.environ.get("CHAN_DESK_MOCK_N", 160))
MOCK_THEMES = ["AI算力", "机器人", "低空经济", "固态电池", "创新药", "半导体",
               "光伏", "军工", "消费电子", "数据要素", "卫星互联网", "有色金属"]
_WORLD_DAYS = 6000


def _mock_days(start, end):
    days = pd.bdate_range(start, end)
    return [d.strftime("%Y-%m-%d") for d in days]


@lru_cache(None)
def _mock_world():
    rng = np.random.default_rng(7)
    s = np.zeros(_WORLD_DAYS)
    active = np.zeros((_WORLD_DAYS, 2), int)
    i = 0
    while i < _WORLD_DAYS:
        n = int(rng.integers(50, 110))
        seg = np.sin(np.linspace(-np.pi / 2, 1.5 * np.pi, n)) * rng.uniform(0.6, 1.0)
        s[i:i + n] = seg[:_WORLD_DAYS - i]
        active[i:i + n] = rng.choice(len(MOCK_THEMES), 2, replace=False)
        i += n
    return s + rng.normal(0, 0.08, _WORLD_DAYS), active


def mock_themes_of(code):
    rng = np.random.default_rng(int(hashlib.md5((code + "t").encode()).hexdigest()[:8], 16))
    return sorted(set(int(x) for x in rng.choice(len(MOCK_THEMES), int(rng.integers(1, 4)), replace=False)))


def _mock_series(code, days):
    from .limits import limit_pct
    seed = int(hashlib.md5(code.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    world, active = _mock_world()
    mine = set(mock_themes_of(code))
    p = 5 + rng.random() * 40
    drift = rng.normal(0.0002, 0.0006)
    out = []
    regime = 0.0
    board, hype = 0, 0
    for k, d in enumerate(days):
        sk = world[k]
        hot = max(sk, 0.0)
        act = bool(mine & set(active[k]))
        lim = limit_pct(code, d)
        if rng.random() < 0.02:
            regime = rng.normal(0, 0.004)
        if board > 0:
            p_zt = 0.10 + hot * (0.55 if act else 0.25)
        else:
            p_zt = 0.002 + 0.006 * hot + (0.05 * hot if act else 0.0)
        p_dt = 0.001 + (0.08 if hype > 0 else 0.006) * max(-sk, 0.0)
        u = rng.random()
        o = p * (1 + rng.normal(0, 0.006))
        if u < p_zt:
            c = round(p * (1 + lim), 2)
            o = min(o, c)
            h = c
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.01)))
            board += 1
        elif u < p_zt + p_dt:
            c = round(p * (1 - lim), 2)
            o = max(o, c)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.005)))
            lo = c
            board = 0
        else:
            r = rng.normal(drift + regime + 0.004 * sk, 0.022)
            r = max(min(r, lim - 0.012), -lim + 0.012)
            c = round(p * (1 + r), 2)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.008)))
            if rng.random() < p_zt * 0.4:      # 炸板：盘中摸过涨停
                h = round(p * (1 + lim), 2)
            h = min(h, round(p * (1 + lim), 2))
            lo = max(min(o, c) * (1 - abs(rng.normal(0, 0.008))), round(p * (1 - lim), 2))
            board = 0
        hype = 15 if board >= 2 else max(hype - 1, 0)
        o = min(max(o, lo), h)
        v = float(rng.integers(2e5, 5e6)) * (1 + abs(c / p - 1) * 10)
        out.append((d, round(o, 2), round(h, 2), round(lo, 2), c, v, v * c, rng.random() * 5,
                    round((c / p - 1) * 100, 4)))
        p = c
    return pd.DataFrame(out, columns=["date", "open", "high", "low", "close", "volume", "amount", "turn", "pctChg"])


def mock_stock_list():
    res = []
    for i in range(MOCK_N):
        code = f"sh.60{i:04d}" if i % 3 == 0 else (f"sz.00{i:04d}" if i % 3 == 1 else f"sz.30{i:04d}")
        res.append((code, f"模拟股票{i:03d}", "2010-01-01"))
    return res


@lru_cache(maxsize=512)
def _mock_full(code, end):
    return _mock_series(code, _mock_days(DATA_START, end))


def mock_daily(code, start, end, index=False):
    days = _mock_days(DATA_START, end)
    if index:
        rng = np.random.default_rng(300)
        world, _ = _mock_world()
        r = rng.normal(0.0002, 0.011, len(days)) + 0.002 * world[:len(days)]
        c = 3500 * np.cumprod(1 + r)
        df = pd.DataFrame({"date": days, "open": c, "high": c * 1.005, "low": c * 0.995, "close": c})
    else:
        df = _mock_full(code, days[-1] if days else end).copy()
        df["tradestatus"] = "1"
        df["isST"] = "1" if code in ("sh.600003", "sz.000004") else "0"
    return df[(df["date"] >= start) & (df["date"] <= end)].copy()


def mock_30m(code, start, end):
    daily = mock_daily(code, start, end)
    rng = np.random.default_rng(1)
    rows = []
    slots = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    for r in daily.itertuples():
        path = np.linspace(r.open, r.close, 9)[1:] * (1 + rng.normal(0, 0.003, 8))
        prev = r.open
        for s_, c in zip(slots, path):
            c = min(max(c, r.low), r.high)
            rows.append((f"{r.date} {s_}", prev, max(prev, c), min(prev, c), c, r.volume / 8))
            prev = c
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def stock_list():
    if MOCK:
        return mock_stock_list()
    return bs_stock_list() if DATA_SOURCE == "baostock" else tc_stock_list()
# ---------------------------------------------------------------- 腾讯 + 新浪（默认来源）
def tc_stock_list():
    from . import sources as S
    return [(code, name, None) for code, name in S.sina_stock_list() if code[:5] in BOARD_PREFIX]


def tc_daily(code, start, end, index=False):
    """不复权日线 + 新浪等比前复权因子 → 和 bs_daily 同样格式的 DataFrame（前复权，最新一天 = 不复权价）。"""
    from . import sources as S
    pre = (dt.date.fromisoformat(start) - dt.timedelta(days=15)).isoformat()   # 多取几天，用来算第一根的涨跌幅
    rows, name = S.tc_daily_raw(code, pre, end)
    cols = ["date", "open", "high", "low", "close", "volume", "amount", "turn", "pctChg", "tradestatus", "isST"]
    if not rows:
        return pd.DataFrame(columns=cols)
    facs = [] if index else S.sina_factors(code)
    last_f = facs[-1][1] if facs else 1.0
    out, prev_c = [], None
    for r in rows:
        k = (S.factor_on(facs, r["date"]) / last_f) if facs else 1.0
        o, h, lo, c = (r[x] / k for x in ("open", "high", "low", "close"))
        out.append((r["date"], o, h, lo, c, r["volume"], r["amount"], r["turn"],
                    (c / prev_c - 1) * 100 if prev_c else None, "1",
                    "1" if name and "ST" in name.upper() else "0"))
        prev_c = c
    df = pd.DataFrame(out, columns=cols)
    return df[df["date"] >= start].reset_index(drop=True)


def tc_30m(code, start, end):
    """腾讯只给最近 320 根 30 分钟线（约 40 个交易日）。"""
    from . import sources as S
    rows = S.tc_minutes(code, "m30")
    df = pd.DataFrame([(f"{d} {t}", o, h, lo, c, v) for d, t, o, h, lo, c, v in rows],
                      columns=["time", "open", "high", "low", "close", "volume"])
    return df[(df["time"].str[:10] >= start) & (df["time"].str[:10] <= end)]


if MOCK:
    fetch_daily, fetch_30m = mock_daily, mock_30m
elif DATA_SOURCE == "baostock":
    fetch_daily, fetch_30m = bs_daily, bs_30m
else:
    fetch_daily, fetch_30m = tc_daily, tc_30m


# ---------------------------------------------------------------- update
def _update_one(args):
    code, last_date, last_close, end = args
    try:
        if last_date and last_close is not None:
            df = fetch_daily(code, last_date, end)
            if not df.empty and df.iloc[0]["date"] == last_date and last_close:
                if abs(float(df.iloc[0]["close"]) - last_close) > max(0.011, last_close * 1e-4):
                    df = fetch_daily(code, DATA_START, end)   # 发生除权，重拉全部
                    return code, df, True, None
            return code, df, False, None
        return code, fetch_daily(code, DATA_START, end), True, None
    except Exception as e:  # noqa
        return code, None, False, str(e)


def _save_daily(conn, code, df, full):
    if df is None or df.empty:
        return
    if "tradestatus" in df:
        is_st = int(df["isST"].iloc[-1] == "1")
        df = df[df["tradestatus"] == "1"]
        conn.execute("UPDATE stocks SET is_st=? WHERE code=?", (is_st, code))
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["high"] > 0) & (df["low"] > 0)]
    pct = (pd.to_numeric(df["pctChg"], errors="coerce") / 100) if "pctChg" in df else pd.Series(None, index=df.index)
    st = (df["isST"] == "1").astype(int) if "isST" in df else pd.Series(0, index=df.index)
    if full:
        conn.execute("DELETE FROM kline_d WHERE code=?", (code,))
    conn.executemany(
        "INSERT OR REPLACE INTO kline_d(code,date,open,high,low,close,volume,amount,turn,pct_chg,is_st) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(code, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount, r.turn,
          None if pd.isna(pc) else float(pc), int(si)) for r, pc, si in zip(df.itertuples(), pct, st)],
    )
    row = conn.execute("SELECT MAX(date), COUNT(*), MIN(date) FROM kline_d WHERE code=?", (code,)).fetchone()
    conn.execute("UPDATE stocks SET last_date=?, bars=? WHERE code=?", (row[0], row[1], code))
    if full and row[2] and row[2] > (dt.date.fromisoformat(DATA_START) + dt.timedelta(days=10)).isoformat():
        conn.execute("UPDATE stocks SET ipo_date=? WHERE code=? AND (ipo_date IS NULL OR ipo_date='')", (row[2], code))
    # 流通股本 ≈ 成交量 / 换手率，供盘后包增量更新时推算换手率
    if "turn" in df:
        t = pd.to_numeric(df["turn"], errors="coerce").tail(20)
        v = pd.to_numeric(df["volume"], errors="coerce").tail(20)
        fs = (v / (t / 100)).where(t > 0).dropna()
        if len(fs):
            conn.execute("UPDATE stocks SET float_sh=? WHERE code=?", (float(fs.median()), code))


def last_closed_day():
    """北京时间 15:30 以前，今天的K线还没走完，只更新到昨天。"""
    now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    d = now.date() if now.time() >= dt.time(15, 30) else now.date() - dt.timedelta(days=1)
    return d.isoformat()


def update_market(progress=lambda *a, **k: None, end=None):
    """下载/增量更新全部 A 股日线、基准指数和交易日历。"""
    db.init_db()
    end = end or (today() if MOCK else last_closed_day())
    progress("stage", "获取股票列表")
    lst = stock_list()
    with db.session() as c:
        c.executemany("INSERT OR IGNORE INTO stocks(code,name,ipo_date) VALUES(?,?,?)", lst)
        c.executemany("UPDATE stocks SET name=? WHERE code=?", [(n, code) for code, n, _ in lst])
        # 最后一根K线没有涨跌幅（旧版本下载的）→ 视为需要整只重新下载
        known = {r[0]: (r[1] if r[3] is not None else None, r[2] if r[3] is not None else None) for r in c.execute(
            "SELECT s.code, s.last_date, k.close, k.pct_chg FROM stocks s LEFT JOIN kline_d k "
            "ON k.code=s.code AND k.date=s.last_date")}
    # 基准指数 + 交易日历
    progress("stage", "下载基准指数")
    idx = fetch_daily(BENCHMARK, DATA_START, end, index=True)
    if MOCK:
        idx = idx[["date", "open", "high", "low", "close"]]
    with db.session() as c:
        c.executemany("INSERT OR REPLACE INTO index_d VALUES(?,?,?,?,?,?)",
                      [(BENCHMARK, r.date, r.open, r.high, r.low, r.close) for r in idx.itertuples()])
        c.executemany("INSERT OR IGNORE INTO trade_days VALUES(?)", [(d,) for d in idx["date"]])
    for icode in MARKET_INDEXES:          # 决策台的指数过滤；失败不影响其余步骤
        if icode == BENCHMARK:
            continue
        try:
            ix = fetch_daily(icode, DATA_START, end, index=True)
            with db.session() as c:
                c.executemany("INSERT OR REPLACE INTO index_d VALUES(?,?,?,?,?,?)",
                              [(icode, r.date, r.open, r.high, r.low, r.close) for r in ix.itertuples()])
        except Exception as e:  # noqa
            progress("stage", f"指数 {icode} 下载失败：{e}")
    latest_day = idx["date"].max() if not idx.empty else end

    codes = [code for code, _, _ in lst]
    if not MOCK and DATA_SOURCE != "baostock":
        known = _tdx_fast_path(known, {c: n for c, n, _ in lst}, latest_day, progress)
    tasks = [(code, known.get(code, (None, None))[0], known.get(code, (None, None))[1], end)
             for code in codes if known.get(code, (None,))[0] != latest_day]
    progress("stage", f"下载日线（{len(tasks)} 只需要更新）")
    done, errors = 0, []
    total = len(tasks)
    conn = db.connect()
    try:
        workers = 1 if MOCK else DOWNLOAD_WORKERS
        if workers == 1:
            it = (_update_one(t) for t in tasks)
            for code, df, full, err in it:
                done += 1
                if err:
                    errors.append(f"{code}: {err}")
                else:
                    _save_daily(conn, code, df, full)
                if done % 20 == 0 or done == total:
                    conn.commit()
                    progress("download", done=done, total=total)
        else:
            with ProcessPoolExecutor(workers) as ex:
                futs = [ex.submit(_update_one, t) for t in tasks]
                for f in as_completed(futs):
                    code, df, full, err = f.result()
                    done += 1
                    if err:
                        errors.append(f"{code}: {err}")
                    else:
                        _save_daily(conn, code, df, full)
                    if done % 20 == 0 or done == total:
                        conn.commit()
                        progress("download", done=done, total=total)
        conn.commit()
    finally:
        conn.close()
    db.set_meta("last_market_update", {"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                       "latest_day": latest_day, "errors": errors[:50]})
    return latest_day, errors


def _tdx_fast_path(known, names, latest_day, progress):
    """日常增量：用通达信盘后包一次补齐全市场缺的几天，避免逐只请求腾讯。
    盘后包里的昨收已经按除权调整过：昨收和库里最后收盘价对不上 = 发生除权，这只交给腾讯整只重下。"""
    from . import sources as S
    have = [v[0] for v in known.values() if v[0]]
    if not have:
        return known
    days = db.trade_days()
    last_common = max(set(have), key=have.count)       # 大多数股票停在的那一天
    todo = [d for d in days if last_common < d <= latest_day]
    if not todo or len(todo) > 10:
        return known
    known = dict(known)
    with db.session() as c:
        fsh = {a: b for a, b in c.execute("SELECT code, float_sh FROM stocks")}
    added = 0
    for d in todo:
        progress("stage", f"下载通达信盘后包 {d}")
        try:
            pkg = S.tdx_package(d)
        except Exception as e:  # noqa
            progress("stage", f"盘后包 {d} 下载失败（{e}），改为逐只下载")
            break
        if pkg is None:
            progress("stage", f"{d} 的盘后包还没发布（通常收盘后几小时），改为逐只下载")
            break
        prev = days[days.index(d) - 1]
        rows, exdiv = [], 0
        for code, (ld, lc) in list(known.items()):
            r = pkg.get(code)
            if ld != prev or lc is None or r is None:
                continue
            if abs(r["prev_close"] - lc) > max(0.011, lc * 2e-4):
                known[code] = (None, None)      # 除权：整只重下
                exdiv += 1
                continue
            name = names.get(code, r["name"])
            turn = r["volume"] / fsh[code] * 100 if fsh.get(code) else None
            rows.append((code, d, r["open"], r["high"], r["low"], r["close"], r["volume"], r["amount"], turn,
                         r["close"] / r["prev_close"] - 1 if r["prev_close"] else None,
                         int("ST" in (name or "").upper())))
            known[code] = (d, r["close"])
        with db.session() as c:
            c.executemany("INSERT OR REPLACE INTO kline_d(code,date,open,high,low,close,volume,amount,turn,pct_chg,"
                          "is_st) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            c.executemany("UPDATE stocks SET last_date=?, bars=bars+1 WHERE code=?", [(d, r[0]) for r in rows])
        added += len(rows)
        progress("stage", f"盘后包 {d}：更新 {len(rows)} 只，除权 {exdiv} 只待重下")
    return known


def ensure_30m(code, start, end):
    """按需下载并缓存 30 分钟K线，返回 DataFrame。"""
    have = db.load_30m(code, start, end)
    if not have.empty and have["time"].iloc[0][:10] <= start and have["time"].iloc[-1][:10] >= end:
        return have
    df = fetch_30m(code, start, end)
    if df is not None and not df.empty:
        with db.session() as c:
            c.executemany("INSERT OR REPLACE INTO kline_30m VALUES(?,?,?,?,?,?,?)",
                          [(code, r.time, r.open, r.high, r.low, r.close, r.volume) for r in df.itertuples()])
    return db.load_30m(code, start, end)


# ---------------------------------------------------------------- 分时封板（首封时间、开板次数）
# 用 baostock 5 分钟线（不复权）推算：涨停价 = 当天最高价；开盘即涨停记为 09:25；
# 否则首封 = 第一根收在涨停价的 5 分钟K线的结束时间；开板次数 = 封住后又有成交低于涨停价的段数。
# 只是近似（5 分钟粒度看不到秒级的开板），够用来区分“早封 / 午后封 / 尾盘封、封得牢 / 反复开板”。
def seal_stats(bars):
    """bars: [(hhmm, open, high, low, close), ...] 一天的 5 分钟线，按时间排序。"""
    if not bars:
        return None
    lim = max(b[2] for b in bars)
    tol = 0.0051
    at = [b[4] >= lim - tol for b in bars]
    if not at[-1]:
        return None                      # 收盘没封住（不是涨停）
    if bars[0][1] >= lim - tol:
        first = "09:25"
        sealed_from = 0
    else:
        sealed_from = at.index(True)
        first = bars[sealed_from][0]
    opens, sealed, last = 0, True, first
    for i in range(sealed_from + 1, len(bars)):
        broke = bars[i][3] < lim - tol
        if sealed and broke:
            opens += 1
            sealed = False
        if not sealed and at[i] and not broke:
            sealed, last = True, bars[i][0]
        elif not sealed and at[i]:
            sealed, last = True, bars[i][0]
    return first, last, opens


def bs_5m_bars(code, day):
    bs = _BS.ensure()
    rs = bs.query_history_k_data_plus(code, "time,open,high,low,close", start_date=day, end_date=day,
                                      frequency="5", adjustflag="3")
    df = _rs_to_df(rs)
    if df.empty:
        return []
    t = df["time"].str
    hm = (t[8:10] + ":" + t[10:12]).tolist()
    vals = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce").values.tolist()
    return [(h, *v) for h, v in zip(hm, vals)]


def mock_5m_bars(code, day):
    full = _mock_full(code, today())
    d = full[full["date"] == day]
    if d.empty:
        return []
    r = d.iloc[0]
    rng = np.random.default_rng(int(hashlib.md5((code + day).encode()).hexdigest()[:8], 16))
    times = [f"{h:02d}:{m:02d}" for h, m in
             [(9, 35 + 5 * i) for i in range(5)] + [(10, 5 * i) for i in range(12)] + [(11, 5 * i) for i in range(7)]
             + [(13, 5 + 5 * i) for i in range(11)] + [(14, 5 * i) for i in range(12)] + [(15, 0)]]
    n = len(times)
    lim = r.high
    if r.open >= lim - 0.005 and r.low >= lim - 0.005:
        return [(t, lim, lim, lim, lim) for t in times]
    k = int(min(n - 1, abs(rng.normal(0.25, 0.3)) * n))
    path = list(np.linspace(r.open, lim, k + 1))[1:] + [lim] * (n - k)
    out = []
    for i, (t, c) in enumerate(zip(times, path[:n])):
        lo = c * (1 - abs(rng.normal(0, 0.004))) if (c < lim or rng.random() < 0.06) else c
        out.append((t, c, c, max(lo, r.low), c))
    return out


def _seal_one(args):
    code, day = args
    try:
        bars = (mock_5m_bars if MOCK else bs_5m_bars)(code, day)
        return code, day, seal_stats(bars), None
    except Exception as e:  # noqa
        _BS.logged = False            # 下次调用重新登录
        return code, day, None, str(e)


def _seals_cn(tasks, progress, save, conn):
    """同花顺涨停揭秘（按天一次拿全市场，含涨停原因）为主，腾讯 5 分钟线（最近约 6 个交易日）补缺。"""
    from . import sources as S
    by_day = {}
    for code, day in tasks:
        by_day.setdefault(day, []).append(code)
    recent = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    m5_from = db.trade_days()[-6:][0] if db.trade_days() else today()
    m5_cache, done = {}, 0
    for day in sorted(by_day):
        ths = {}
        if day >= recent:
            try:
                for it in S.ths_limit_up(day):
                    ths[it["code"]] = it
                conn.executemany("INSERT OR REPLACE INTO zt_reason VALUES(?,?,?,?,?)",
                                 [(it["code"], day, it["reason"], it["board_type"], it["high_days"])
                                  for it in ths.values()])
            except Exception as e:  # noqa
                progress("stage", f"同花顺涨停池 {day} 获取失败：{e}")
        for code in by_day[day]:
            it = ths.get(code)
            if it and it["first_time"]:
                first = "09:25" if it["first_time"] <= "09:30" else it["first_time"]
                save(code, day, (first, it["last_time"] or first, it["open_num"]), None)
            elif day >= m5_from:
                try:
                    if code not in m5_cache:
                        m5_cache[code] = S.tc_minutes(code, "m5")
                    bars = [(t, o, h, lo, c) for d, t, o, h, lo, c, _ in m5_cache[code] if d == day]
                    save(code, day, seal_stats(bars), None)
                except Exception as e:  # noqa
                    save(code, day, None, str(e))
            else:
                save(code, day, None, "太早，没有分时数据")
            done += 1
        conn.commit()
        progress("seal", done=done, total=len(tasks))


def update_seals(pairs, progress=lambda *a, **k: None):
    """下载 (code, date) 对的分时封板数据，已有的跳过。返回 (成功数, 失败数)。"""
    db.init_db()
    with db.session() as c:
        have = set(c.execute("SELECT code, date FROM zt_seal").fetchall())
    tasks = [p for p in dict.fromkeys(pairs) if p not in have]
    if not tasks:
        return 0, 0
    progress("stage", f"下载分时封板数据（{len(tasks)} 个涨停）")
    done, ok_, bad = 0, 0, 0
    conn = db.connect()

    def save(code, day, st, err):
        nonlocal ok_, bad
        if err or st is None:
            bad += 1
            # 拿不到或当天没封住也记一行，避免反复下载
            conn.execute("INSERT OR REPLACE INTO zt_seal VALUES(?,?,?,?,?)", (code, day, None, None, None))
        else:
            ok_ += 1
            conn.execute("INSERT OR REPLACE INTO zt_seal VALUES(?,?,?,?,?)", (code, day, *st))

    try:
        workers = 1 if MOCK else DOWNLOAD_WORKERS
        if not MOCK and DATA_SOURCE != "baostock":
            _seals_cn(tasks, progress, save, conn)
        elif workers == 1:
            for t in tasks:
                save(*_seal_one(t))
                done += 1
                if done % 50 == 0 or done == len(tasks):
                    conn.commit()
                    progress("seal", done=done, total=len(tasks))
        else:
            with ProcessPoolExecutor(workers) as ex:
                for f in as_completed([ex.submit(_seal_one, t) for t in tasks]):
                    save(*f.result())
                    done += 1
                    if done % 50 == 0 or done == len(tasks):
                        conn.commit()
                        progress("seal", done=done, total=len(tasks))
        conn.commit()
    finally:
        conn.close()
    return ok_, bad
