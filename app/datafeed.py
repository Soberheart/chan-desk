"""行情下载：baostock（真实）或合成数据（MOCK 模式，离线测试用）。

日线使用前复权（adjustflag=2）。增量更新时，如果最后一根已存K线的收盘价和新拉到的不一致，
说明发生了除权，会自动重新下载该股票全部历史。
"""
import datetime as dt
import hashlib
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

from . import db
from .settings import BENCHMARK, DATA_START, DOWNLOAD_WORKERS, MOCK

BOARD_PREFIX = ("sh.60", "sh.68", "sz.00", "sz.30")
DAY_FIELDS = "date,open,high,low,close,volume,amount,turn,tradestatus,isST"


def today():
    return dt.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- baostock
class _BS:
    """每个进程各自登录一次 baostock。"""
    logged = False

    @classmethod
    def ensure(cls):
        import baostock as bs
        if not cls.logged:
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
    for attempt in range(3):
        rs = bs.query_history_k_data_plus(code, fields, start_date=start, end_date=end,
                                          frequency="d", adjustflag="3" if index else "2")
        if rs.error_code == "0":
            break
        _BS.logged = False
        bs = _BS.ensure()
    else:
        raise RuntimeError(f"{code}: {rs.error_msg}")
    df = _rs_to_df(rs)
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
MOCK_N = 80


def _mock_days(start, end):
    days = pd.bdate_range(start, end)
    return [d.strftime("%Y-%m-%d") for d in days]


def _mock_series(code, days):
    seed = int(hashlib.md5(code.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    p = 5 + rng.random() * 40
    drift = rng.normal(0.0002, 0.0006)
    out = []
    regime = 0.0
    for d in days:
        if rng.random() < 0.02:
            regime = rng.normal(0, 0.004)
        r = rng.normal(drift + regime, 0.022)
        r = max(min(r, 0.099), -0.099)
        o = p * (1 + rng.normal(0, 0.006))
        c = p * (1 + r)
        h = max(o, c) * (1 + abs(rng.normal(0, 0.008)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.008)))
        v = float(rng.integers(2e5, 5e6)) * (1 + abs(r) * 10)
        out.append((d, round(o, 2), round(h, 2), round(lo, 2), round(c, 2), v, v * c, rng.random() * 5))
        p = c
    return pd.DataFrame(out, columns=["date", "open", "high", "low", "close", "volume", "amount", "turn"])


def mock_stock_list():
    res = []
    for i in range(MOCK_N):
        code = f"sh.60{i:04d}" if i % 2 == 0 else f"sz.00{i:04d}"
        res.append((code, f"模拟股票{i:02d}", "2010-01-01"))
    return res


def mock_daily(code, start, end, index=False):
    days = _mock_days(DATA_START, end)
    df = _mock_series(code, days)
    df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    if not index:
        df["tradestatus"] = "1"
        df["isST"] = "0"
    return df


def mock_30m(code, start, end):
    daily = mock_daily(code, start, end)
    rng = np.random.default_rng(1)
    rows = []
    slots = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    for r in daily.itertuples():
        path = np.linspace(r.open, r.close, 9)[1:] * (1 + rng.normal(0, 0.003, 8))
        prev = r.open
        for s, c in zip(slots, path):
            c = min(max(c, r.low), r.high)
            rows.append((f"{r.date} {s}", prev, max(prev, c), min(prev, c), c, r.volume / 8))
            prev = c
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    for d, g in df.groupby(df["time"].str[:10]):
        pass
    return df


stock_list = mock_stock_list if MOCK else bs_stock_list
fetch_daily = mock_daily if MOCK else bs_daily
fetch_30m = mock_30m if MOCK else bs_30m


# ---------------------------------------------------------------- update
def _update_one(args):
    code, last_date, last_close, end = args
    try:
        if last_date:
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
    if full:
        conn.execute("DELETE FROM kline_d WHERE code=?", (code,))
    conn.executemany(
        "INSERT OR REPLACE INTO kline_d VALUES(?,?,?,?,?,?,?,?,?)",
        [(code, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount, r.turn) for r in df.itertuples()],
    )
    row = conn.execute("SELECT MAX(date), COUNT(*) FROM kline_d WHERE code=?", (code,)).fetchone()
    conn.execute("UPDATE stocks SET last_date=?, bars=? WHERE code=?", (row[0], row[1], code))


def update_market(progress=lambda *a, **k: None, end=None):
    """下载/增量更新全部 A 股日线、基准指数和交易日历。"""
    db.init_db()
    end = end or today()
    progress("stage", "获取股票列表")
    lst = stock_list()
    with db.session() as c:
        c.executemany("INSERT OR IGNORE INTO stocks(code,name,ipo_date) VALUES(?,?,?)", lst)
        c.executemany("UPDATE stocks SET name=? WHERE code=?", [(n, code) for code, n, _ in lst])
        known = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT s.code, s.last_date, k.close FROM stocks s LEFT JOIN kline_d k "
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
    latest_day = idx["date"].max() if not idx.empty else end

    codes = [code for code, _, _ in lst]
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
