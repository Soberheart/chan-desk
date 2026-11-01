"""选股、历史验证、组合回测。全部基于“发现日”的时点信号，不使用未来数据。"""
import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from . import db
from .settings import BENCHMARK, COMMISSION, MIN_BARS, SLIPPAGE, STAMP_TAX

BOARDS = {"sh60": "沪主板", "sz00": "深主板", "sz30": "创业板", "sh68": "科创板"}


def board_of(code):
    return code[:2] + code[3:5]


def _type_filter_sql(types):
    if not types:
        return "", []
    parts = " OR ".join(["(','||s.types||',') LIKE ?"] * len(types))
    return f" AND ({parts})", [f"%,{t},%" for t in types]


def latest_day():
    with db.session() as c:
        r = c.execute("SELECT MAX(date) FROM trade_days").fetchone()
    return r[0]


def index_closes():
    with db.session() as c:
        rows = c.execute("SELECT date, close FROM index_d WHERE code=? ORDER BY date", (BENCHMARK,)).fetchall()
    return [r[0] for r in rows], np.array([r[1] for r in rows], float)


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ---------------------------------------------------------------- 选股
def select(date=None, window=3, min_score=0, types=None, boards=None, exclude_st=True, limit=300):
    date = date or latest_day()
    days = db.trade_days(end=date)[-window:]
    if not days:
        return {"date": date, "rows": []}
    tsql, targs = _type_filter_sql(types)
    sql = ("SELECT s.code, st.name, s.detect_date, s.bsp_date, s.types, s.score, s.detail, s.bsp_price, "
           "s.detect_close, st.is_st, st.bars, k.close FROM signals s JOIN stocks st ON st.code=s.code "
           "LEFT JOIN kline_d k ON k.code=s.code AND k.date=? "
           "WHERE s.is_buy=1 AND s.detect_date>=? AND s.detect_date<=? AND s.score>=?" + tsql +
           " ORDER BY s.score DESC")
    with db.session() as c:
        rows = c.execute(sql, [date, days[0], date, min_score] + targs).fetchall()
    best = {}
    for r in rows:
        code = r[0]
        if code in best:
            continue
        if exclude_st and r[9]:
            continue
        if r[10] is not None and r[10] < MIN_BARS:
            continue
        if boards and board_of(code) not in boards:
            continue
        detail = json.loads(r[6]) if r[6] else {}
        cur = r[11]
        best[code] = {
            "code": code, "name": r[1], "detect_date": r[2], "bsp_date": r[3], "types": r[4],
            "score": r[5], "detail": detail.get("分项", {}), "features": detail.get("特征", {}),
            "bsp_price": r[7], "close": cur,
            "gain": _clean(cur / r[7] - 1) if cur and r[7] else None,
            "board": BOARDS.get(board_of(code), ""),
        }
    out = sorted(best.values(), key=lambda x: -x["score"])[:limit]
    return {"date": date, "days": days, "rows": out}


# ---------------------------------------------------------------- 历史验证
def validate(start, end, min_score=60, top_n=10, types=None, entry="close", boards=None, exclude_st=True):
    tsql, targs = _type_filter_sql(types)
    pre = "o" if entry == "open" else ""
    sql = (f"SELECT s.code, st.name, s.detect_date, s.types, s.score, s.{pre}ret5, s.{pre}ret10, s.{pre}ret20, "
           "s.mdd10, s.mfe10, st.is_st FROM signals s JOIN stocks st ON st.code=s.code "
           "WHERE s.is_buy=1 AND s.detect_date>=? AND s.detect_date<=? AND s.score>=?" + tsql +
           " ORDER BY s.detect_date, s.score DESC")
    with db.session() as c:
        rows = c.execute(sql, [start, end, min_score] + targs).fetchall()
    df = pd.DataFrame(rows, columns=["code", "name", "date", "types", "score", "r5", "r10", "r20",
                                     "mdd10", "mfe10", "is_st"])
    if exclude_st:
        df = df[df["is_st"] != 1]
    if boards:
        df = df[df["code"].map(board_of).isin(boards)]
    df = df.drop_duplicates(["date", "code"])
    df = df.groupby("date", group_keys=False).head(top_n)
    if df.empty:
        return {"empty": True}

    idx_dates, idx_close = index_closes()
    pos = {d: i for i, d in enumerate(idx_dates)}

    def bench(d, h):
        i = pos.get(d)
        if i is None or i + h >= len(idx_close):
            return np.nan
        return idx_close[i + h] / idx_close[i] - 1

    for h in (5, 10, 20):
        df[f"b{h}"] = df["date"].map(lambda d, h=h: bench(d, h))
    df["main_type"] = df["types"].str.split(",").str[0]
    df["bucket"] = pd.cut(df["score"], [0, 50, 60, 70, 80, 90, 101], right=False,
                          labels=["<50", "50-60", "60-70", "70-80", "80-90", "90+"]).astype(str)

    def agg(g):
        res = {"n": int(len(g))}
        for h in (5, 10, 20):
            s = g[f"r{h}"].dropna()
            b = g[f"b{h}"].dropna()
            res[f"avg{h}"] = _clean(float(s.mean())) if len(s) else None
            res[f"med{h}"] = _clean(float(s.median())) if len(s) else None
            res[f"win{h}"] = _clean(float((s > 0).mean())) if len(s) else None
            res[f"bench{h}"] = _clean(float(b.mean())) if len(b) else None
            res[f"excess{h}"] = (res[f"avg{h}"] - res[f"bench{h}"]) if len(s) and len(b) else None
        res["mdd10"] = _clean(float(g["mdd10"].dropna().mean())) if g["mdd10"].notna().any() else None
        return res

    summary = agg(df)
    by_bucket = [{"key": k, **agg(g)} for k, g in df.groupby("bucket")]
    order = ["1", "1p", "2", "2s", "3a", "3b"]
    by_type = sorted([{"key": k, **agg(g)} for k, g in df.groupby("main_type")],
                     key=lambda x: order.index(x["key"]) if x["key"] in order else 9)
    df["month"] = df["date"].str[:7]
    by_month = [{"key": k, **agg(g)} for k, g in df.groupby("month")]
    picks = df.sort_values(["date", "score"], ascending=[False, False]).head(2000)
    picks = [{k: _clean(v) for k, v in r.items()} for r in
             picks[["date", "code", "name", "types", "score", "r5", "r10", "r20", "mdd10", "mfe10"]].to_dict("records")]
    return {"summary": summary, "by_bucket": by_bucket, "by_type": by_type, "by_month": by_month, "picks": picks}


# ---------------------------------------------------------------- 组合回测
class _Prices:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.cache = {}

    def get(self, code):
        if code not in self.cache:
            with db.session() as c:
                rows = c.execute("SELECT date, open, high, low, close FROM kline_d WHERE code=? AND date>=? "
                                 "AND date<=? ORDER BY date", (code, self.start, self.end)).fetchall()
            d = {r[0]: r[1:] for r in rows}
            dates = [r[0] for r in rows]
            self.cache[code] = (d, dates)
        return self.cache[code]

    def bar(self, code, day):
        return self.get(code)[0].get(day)

    def prev_close(self, code, day):
        d, dates = self.get(code)
        import bisect
        i = bisect.bisect_left(dates, day)
        return d[dates[i - 1]][3] if i > 0 else None


def backtest(start, end, min_score=70, max_pos=5, types=None, stop_pct=0.08, use_bsp_stop=True,
             take_profit=0.0, max_hold=20, exit_on_sell=True, capital=1_000_000, boards=None, exclude_st=True):
    days = db.trade_days(start, end)
    if len(days) < 3:
        return {"error": "区间内交易日太少，请检查日期或先更新数据"}
    tsql, targs = _type_filter_sql(types)
    with db.session() as c:
        buys = c.execute("SELECT s.code, s.detect_date, s.score, s.types, s.bsp_price, st.is_st, st.name "
                         "FROM signals s JOIN stocks st ON st.code=s.code WHERE s.is_buy=1 AND s.detect_date>=? "
                         "AND s.detect_date<=? AND s.score>=?" + tsql + " ORDER BY s.detect_date, s.score DESC",
                         [start, end, min_score] + targs).fetchall()
        sells = c.execute("SELECT code, detect_date, types FROM signals WHERE is_buy=0 AND detect_date>=? "
                          "AND detect_date<=?", (start, end)).fetchall()
    buy_by_day = defaultdict(list)
    names = {}
    for code, d, score, types_, bsp_price, is_st, name in buys:
        if exclude_st and is_st:
            continue
        if boards and board_of(code) not in boards:
            continue
        buy_by_day[d].append((code, score, types_, bsp_price))
        names[code] = name
    sell_set = {(c_, d) for c_, d, _ in sells}

    px = _Prices(days[0], days[-1])
    cash = float(capital)
    held = {}            # code -> dict
    pending_buy = []     # [(code, score, types, bsp_price)]
    pending_sell = set()
    trades, equity, npos = [], [], []
    fee_buy = COMMISSION + SLIPPAGE
    fee_sell = COMMISSION + STAMP_TAX + SLIPPAGE

    def sell(code, day, price, reason):
        nonlocal cash
        p = held.pop(code)
        proceeds = p["shares"] * price * (1 - fee_sell)
        cash += proceeds
        trades.append({"code": code, "name": names.get(code, ""), "types": p["types"], "score": p["score"],
                       "buy_date": p["date"], "buy_price": round(p["price"], 3), "sell_date": day,
                       "sell_price": round(price, 3), "days": p["hold"], "reason": reason,
                       "ret": proceeds / p["cost"] - 1})

    for k, day in enumerate(days):
        # 1) 开盘：执行昨天决定的卖出
        for code in list(pending_sell):
            if code in held:
                b = px.bar(code, day)
                if b:
                    sell(code, day, b[0], "出现卖点")
                    pending_sell.discard(code)
            else:
                pending_sell.discard(code)
        # 2) 开盘：执行昨天决定的买入
        if pending_buy:
            eq_now = cash + sum(h["shares"] * h["last"] for h in held.values())
            alloc = eq_now / max_pos
            for code, score, types_, bsp_price in pending_buy:
                if len(held) >= max_pos or code in held:
                    continue
                b = px.bar(code, day)
                pc = px.prev_close(code, day)
                if not b or not pc or b[0] >= pc * 1.095:     # 停牌或一字涨停买不进
                    continue
                price = b[0]
                shares = int(min(alloc, cash) / (price * (1 + fee_buy)) / 100) * 100
                if shares <= 0:
                    continue
                cost = shares * price * (1 + fee_buy)
                cash -= cost
                stop = price * (1 - stop_pct) if stop_pct else 0
                if use_bsp_stop and bsp_price:
                    stop = max(stop, bsp_price * 0.99)
                held[code] = {"date": day, "price": price, "shares": shares, "cost": cost, "stop": stop,
                              "tp": price * (1 + take_profit) if take_profit else None, "hold": 0,
                              "last": price, "score": score, "types": types_}
            pending_buy = []
        # 3) 盘中：止损 / 止盈 / 到期（T+1：当天买的不能卖）
        for code in list(held):
            p = held[code]
            b = px.bar(code, day)
            if not b:
                continue
            o, h, low, c = b
            if p["date"] == day:
                p["last"] = c
                continue
            p["hold"] += 1
            if p["stop"] and low <= p["stop"]:
                sell(code, day, min(o, p["stop"]), "止损")
            elif p["tp"] and h >= p["tp"]:
                sell(code, day, max(o, p["tp"]), "止盈")
            elif max_hold and p["hold"] >= max_hold:
                sell(code, day, c, "到期")
            else:
                p["last"] = c
        # 4) 收盘：记账
        eq = cash + sum(h["shares"] * h["last"] for h in held.values())
        equity.append((day, eq))
        npos.append(len(held))
        # 5) 收盘后：根据当天发现的信号决定明天的操作
        if exit_on_sell:
            for code in held:
                if (code, day) in sell_set:
                    pending_sell.add(code)
        slots = max_pos - len(held)
        if slots > 0 and k + 1 < len(days):
            cands = [x for x in buy_by_day.get(day, []) if x[0] not in held]
            pending_buy = cands[: slots * 2]   # 多备几只，防止涨停买不进

    # 清算未平仓位（按最后收盘价，不计入交易统计）
    open_positions = [{"code": c_, "name": names.get(c_, ""), "buy_date": p["date"], "buy_price": p["price"],
                       "last": p["last"], "ret": p["shares"] * p["last"] * (1 - fee_sell) / p["cost"] - 1}
                      for c_, p in held.items()]

    eq = pd.Series([e for _, e in equity], index=[d for d, _ in equity])
    idx_dates, idx_close = index_closes()
    bench = pd.Series(idx_close, index=idx_dates).reindex(eq.index).ffill()
    bench = bench / bench.iloc[0] * capital if bench.notna().any() else bench
    rets = eq.pct_change().dropna()
    years = max(len(eq) / 244, 1e-9)
    total = eq.iloc[-1] / capital - 1
    dd = (eq / eq.cummax() - 1)
    tr = pd.DataFrame(trades)
    stats = {
        "total_return": total,
        "annual_return": (1 + total) ** (1 / years) - 1 if total > -1 else -1,
        "max_drawdown": float(dd.min()),
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(244)) if rets.std() > 0 else None,
        "bench_return": float(bench.iloc[-1] / capital - 1) if bench.notna().any() else None,
        "trades": int(len(tr)),
        "win_rate": float((tr["ret"] > 0).mean()) if len(tr) else None,
        "avg_ret": float(tr["ret"].mean()) if len(tr) else None,
        "avg_win": float(tr.loc[tr["ret"] > 0, "ret"].mean()) if len(tr) and (tr["ret"] > 0).any() else None,
        "avg_loss": float(tr.loc[tr["ret"] <= 0, "ret"].mean()) if len(tr) and (tr["ret"] <= 0).any() else None,
        "avg_days": float(tr["days"].mean()) if len(tr) else None,
        "exposure": float(np.mean([1 if x else 0 for x in npos])),
    }
    if len(tr):
        wins = tr.loc[tr["ret"] > 0, "ret"].sum()
        losses = -tr.loc[tr["ret"] <= 0, "ret"].sum()
        stats["profit_factor"] = float(wins / losses) if losses > 0 else None
        stats["by_reason"] = tr.groupby("reason")["ret"].agg(["count", "mean"]).reset_index().to_dict("records")
    stats = {k: _clean(v) if not isinstance(v, list) else v for k, v in stats.items()}
    return {
        "stats": stats,
        "curve": {"dates": list(eq.index), "equity": [round(x, 2) for x in eq],
                  "bench": [None if pd.isna(x) else round(x, 2) for x in bench],
                  "drawdown": [round(x, 4) for x in dd]},
        "trades": [{k: _clean(v) for k, v in t.items()} for t in trades[::-1]],
        "open_positions": open_positions,
    }
