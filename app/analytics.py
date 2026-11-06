"""选股、历史验证、组合回测。全部基于“发现日”的时点信号，不使用未来数据。"""
import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from . import db, emotion
from .limits import is_limit_up, limit_pct
from .settings import (BENCHMARK, COMMISSION, EMO, MIN_BARS, MODE_MAX_HOLD, PHASE_CAP, SLIPPAGE, STAMP_TAX,
                       TYPE_NAME)

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
    if isinstance(v, (float, np.floating)) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v


# ---------------------------------------------------------------- 水温闸门
def gate_check(phase, theme_old, hyped, maxb5):
    """92科比的两条禁令落到缠论信号上：主跌不抄老题材；高位震荡回避中位股。返回 (是否放行, 原因)。"""
    if phase == "主跌" and (theme_old or (hyped or 0) >= 2):
        return False, "主跌期不抄老题材"
    lo, hi = EMO["mid_board"]
    if phase == "高位震荡" and maxb5 is not None and lo <= maxb5 <= hi:
        return False, "高位震荡回避中位股"
    return True, ""


def phase_slots(max_pos, phase):
    cap = PHASE_CAP.get(phase, 1.0) if phase else 1.0
    return max(0, int(math.floor(max_pos * cap + 1e-9)))


# ---------------------------------------------------------------- 选股
def select(date=None, window=3, min_score=0, types=None, boards=None, exclude_st=True, limit=300, gate=False):
    date = date or latest_day()
    days = db.trade_days(end=date)[-window:]
    if not days:
        return {"date": date, "rows": []}
    tsql, targs = _type_filter_sql(types)
    sql = ("SELECT s.code, st.name, s.detect_date, s.bsp_date, s.types, s.score, s.detail, s.bsp_price, "
           "s.detect_close, st.is_st, st.bars, k.close, e.phase, e.theme, e.theme_old, e.theme_new, e.hyped, e.maxb5 "
           "FROM signals s JOIN stocks st ON st.code=s.code "
           "LEFT JOIN kline_d k ON k.code=s.code AND k.date=? "
           "LEFT JOIN sig_emo e ON e.code=s.code AND e.date=s.detect_date "
           "WHERE s.is_buy=1 AND s.detect_date>=? AND s.detect_date<=? AND s.score>=?" + tsql +
           " ORDER BY s.score DESC")
    with db.session() as c:
        rows = c.execute(sql, [date, days[0], date, min_score] + targs).fetchall()
    best = {}
    blocked = 0
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
        ok_, why = gate_check(r[12], r[14], r[16], r[17])
        if gate and not ok_:
            blocked += 1
            continue
        detail = json.loads(r[6]) if r[6] else {}
        cur = r[11]
        best[code] = {
            "code": code, "name": r[1], "detect_date": r[2], "bsp_date": r[3], "types": r[4],
            "score": r[5], "detail": detail.get("分项", {}), "features": detail.get("特征", {}),
            "bsp_price": r[7], "close": cur,
            "gain": _clean(cur / r[7] - 1) if cur and r[7] else None,
            "board": BOARDS.get(board_of(code), ""),
            "phase": r[12], "theme": r[13], "theme_old": bool(r[14]), "theme_new": bool(r[15]),
            "hyped": r[16], "gate_ok": ok_, "gate_reason": why,
        }
    out = sorted(best.values(), key=lambda x: -x["score"])[:limit]
    emo = emotion.day_detail(date)
    emo_brief = None
    if emo:
        m = emo["metrics"]
        emo_brief = {"date": emo["date"], "phase": m["phase"], "temp": m["temp"], "cap": emo["cap"],
                     "advice": emo["advice"], "recommend": emo["recommend"]}
    return {"date": date, "days": days, "rows": out, "blocked": blocked, "emotion": emo_brief}


# ---------------------------------------------------------------- 历史验证
def _bench_fn():
    idx_dates, idx_close = index_closes()
    pos = {d: i for i, d in enumerate(idx_dates)}

    def bench(d, h):
        i = pos.get(d)
        if i is None or i + h >= len(idx_close):
            return np.nan
        return idx_close[i + h] / idx_close[i] - 1
    return bench


def _summarize(df, H, mdd_col, type_order, type_label):
    bench = _bench_fn()
    for h in H:
        df[f"b{h}"] = df["date"].map(lambda d, h=h: bench(d, h))
    df["bucket"] = pd.cut(df["score"], [0, 50, 60, 70, 80, 90, 101], right=False,
                          labels=["<50", "50-60", "60-70", "70-80", "80-90", "90+"]).astype(str)
    df["phase"] = df["phase"].fillna("无情绪数据")

    def agg(g):
        res = {"n": int(len(g))}
        for h in H:
            s = g[f"r{h}"].dropna()
            b = g[f"b{h}"].dropna()
            res[f"avg{h}"] = _clean(float(s.mean())) if len(s) else None
            res[f"med{h}"] = _clean(float(s.median())) if len(s) else None
            res[f"win{h}"] = _clean(float((s > 0).mean())) if len(s) else None
            res[f"bench{h}"] = _clean(float(b.mean())) if len(b) else None
            res[f"excess{h}"] = (res[f"avg{h}"] - res[f"bench{h}"]) if len(s) and len(b) else None
        res["mdd"] = _clean(float(g[mdd_col].dropna().mean())) if g[mdd_col].notna().any() else None
        return res

    order_ph = ["低位试错", "主升", "高位震荡", "主跌", "无情绪数据"]
    by_phase = sorted([{"key": k, **agg(g)} for k, g in df.groupby("phase")],
                      key=lambda x: order_ph.index(x["key"]) if x["key"] in order_ph else 9)
    by_type = sorted([{"key": k, "label": type_label(k), **agg(g)} for k, g in df.groupby("main_type")],
                     key=lambda x: type_order.index(x["key"]) if x["key"] in type_order else 9)
    df["month"] = df["date"].str[:7]
    bo = ["<50", "50-60", "60-70", "70-80", "80-90", "90+"]
    by_bucket = sorted([{"key": k, **agg(g)} for k, g in df.groupby("bucket")],
                       key=lambda x: bo.index(x["key"]) if x["key"] in bo else 9)
    return {"summary": agg(df), "by_bucket": by_bucket,
            "by_type": by_type, "by_phase": by_phase,
            "by_month": [{"key": k, **agg(g)} for k, g in df.groupby("month")]}


def _st_filter(df, exclude_st):
    if exclude_st:
        df = df[df["is_st"].fillna(0) != 1]
    return df


def validate(start, end, min_score=60, top_n=10, types=None, entry="close", boards=None, exclude_st=True,
             gate=False, source="chan", modes=None, fit_only=True, buyable_only=True):
    if source == "emotion":
        return validate_emotion(start, end, min_score, top_n, modes, boards, exclude_st, fit_only, buyable_only)
    tsql, targs = _type_filter_sql(types)
    pre = "o" if entry == "open" else ""
    sql = (f"SELECT s.code, st.name, s.detect_date, s.types, s.score, s.{pre}ret5, s.{pre}ret10, s.{pre}ret20, "
           "s.mdd10, s.mfe10, COALESCE(kd.is_st, st.is_st), e.phase, e.theme, e.theme_old, e.hyped, e.maxb5 "
           "FROM signals s JOIN stocks st ON st.code=s.code "
           "LEFT JOIN kline_d kd ON kd.code=s.code AND kd.date=s.detect_date "
           "LEFT JOIN sig_emo e ON e.code=s.code AND e.date=s.detect_date "
           "WHERE s.is_buy=1 AND s.detect_date>=? AND s.detect_date<=? AND s.score>=?" + tsql +
           " ORDER BY s.detect_date, s.score DESC")
    with db.session() as c:
        rows = c.execute(sql, [start, end, min_score] + targs).fetchall()
    df = pd.DataFrame(rows, columns=["code", "name", "date", "types", "score", "r5", "r10", "r20",
                                     "mdd10", "mfe10", "is_st", "phase", "theme", "theme_old", "hyped", "maxb5"])
    df = _st_filter(df, exclude_st)
    if boards:
        df = df[df["code"].map(board_of).isin(boards)]
    blocked = 0
    if gate and len(df):
        okm = [gate_check(p, o, h, m)[0] for p, o, h, m in
               zip(df["phase"], df["theme_old"], df["hyped"], df["maxb5"].where(df["maxb5"].notna(), None))]
        blocked = int(len(df) - sum(okm))
        df = df[okm]
    df = df.drop_duplicates(["date", "code"])
    df = df.groupby("date", group_keys=False).head(top_n)
    if df.empty:
        return {"empty": True}
    df = df.copy()
    df["main_type"] = df["types"].str.split(",").str[0]
    res = _summarize(df, (5, 10, 20), "mdd10", ["1", "1p", "2", "2s", "3a", "3b"], lambda k: TYPE_NAME.get(k, k))
    picks = df.sort_values(["date", "score"], ascending=[False, False]).head(2000)
    picks = [{k: _clean(v) for k, v in r.items()} for r in
             picks[["date", "code", "name", "types", "score", "phase", "theme", "r5", "r10", "r20", "mdd10",
                    "mfe10"]].to_dict("records")]
    return {**res, "picks": picks, "horizons": [5, 10, 20], "source": "chan", "blocked": blocked}


def validate_emotion(start, end, min_score, top_n, modes, boards, exclude_st, fit_only, buyable_only):
    sql = ("SELECT p.code, st.name, p.date, p.mode, p.theme, p.board, p.score, p.fit, p.buyable, "
           "p.oret1, p.oret3, p.oret5, p.oret10, p.mdd5, COALESCE(kd.is_st, st.is_st), d.phase "
           "FROM emo_picks p JOIN stocks st ON st.code=p.code "
           "LEFT JOIN kline_d kd ON kd.code=p.code AND kd.date=p.date "
           "LEFT JOIN emo_daily d ON d.date=p.date "
           "WHERE p.date>=? AND p.date<=? AND p.score>=? ORDER BY p.date, p.score DESC")
    with db.session() as c:
        rows = c.execute(sql, [start, end, min_score]).fetchall()
    df = pd.DataFrame(rows, columns=["code", "name", "date", "mode", "theme", "board", "score", "fit", "buyable",
                                     "r1", "r3", "r5", "r10", "mdd5", "is_st", "phase"])
    df = _st_filter(df, exclude_st)
    if modes:
        df = df[df["mode"].isin(modes)]
    if boards:
        df = df[df["code"].map(board_of).isin(boards)]
    if fit_only:
        df = df[df["fit"] == 1]
    unbuyable = int((df["buyable"] == 0).sum())
    if buyable_only:
        df = df[df["buyable"] == 1]
    df = df.drop_duplicates(["date", "code"]).groupby("date", group_keys=False).head(top_n)
    if df.empty:
        return {"empty": True}
    df = df.copy()
    df["main_type"] = df["mode"]
    res = _summarize(df, (1, 3, 5, 10), "mdd5", ["龙头", "补涨", "切换"], lambda k: k)
    picks = df.sort_values(["date", "score"], ascending=[False, False]).head(2000)
    picks = [{k: _clean(v) for k, v in r.items()} for r in
             picks[["date", "code", "name", "mode", "theme", "board", "score", "phase", "r1", "r3", "r5", "r10",
                    "mdd5"]].to_dict("records")]
    return {**res, "picks": picks, "horizons": [1, 3, 5, 10], "source": "emotion", "unbuyable": unbuyable}


# ---------------------------------------------------------------- 组合回测
class _Prices:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.cache = {}

    def get(self, code):
        if code not in self.cache:
            with db.session() as c:
                rows = c.execute("SELECT date, open, high, low, close, pct_chg, is_st FROM kline_d WHERE code=? "
                                 "AND date>=? AND date<=? ORDER BY date", (code, self.start, self.end)).fetchall()
            d = {r[0]: r[1:] for r in rows}
            dates = [r[0] for r in rows]
            self.cache[code] = (d, dates)
        return self.cache[code]

    def bar(self, code, day):
        return self.get(code)[0].get(day)

    def prev_close(self, code, day):
        import bisect
        d, dates = self.get(code)
        i = bisect.bisect_left(dates, day)
        return d[dates[i - 1]][3] if i > 0 else None

    def pct(self, code, day):
        b = self.bar(code, day)
        if not b:
            return None
        if b[4] is not None:
            return b[4]
        pc = self.prev_close(code, day)
        return b[3] / pc - 1 if pc else None

    def lim(self, code, day):
        b = self.bar(code, day)
        return limit_pct(code, day, bool(b and b[5]))

    def can_buy(self, code, day):
        """停牌或开盘就涨停（一字板）买不进。"""
        b, pc = self.bar(code, day), self.prev_close(code, day)
        return bool(b and pc and b[0] / pc - 1 < self.lim(code, day) - EMO["zt_tol"])

    def can_sell(self, code, day):
        """停牌或一字跌停卖不出。"""
        b = self.bar(code, day)
        if not b:
            return False
        p = self.pct(code, day)
        return not (b[1] <= b[2] * (1 + 1e-6) and p is not None and p <= -(self.lim(code, day) - EMO["zt_tol"]))

    def is_zt(self, code, day):
        b = self.bar(code, day)
        return bool(b and is_limit_up(code, day, bool(b[5]), self.pct(code, day), b[3], b[1]))


def backtest(start, end, min_score=70, max_pos=5, types=None, stop_pct=0.08, use_bsp_stop=True,
             take_profit=0.0, max_hold=20, exit_on_sell=True, capital=1_000_000, boards=None, exclude_st=True,
             strategy="chan", gate=False, phase_cap=False, modes=None, emo_min_score=0, emo_fit_only=True):
    """strategy: chan = 只做缠论买点；emotion = 只做情绪模式（龙头/补涨/切换）；both = 两者都做（情绪优先）。
    gate: 缠论买点过水温闸门；phase_cap: 按情绪阶段限制持仓数（主升满仓、高位震荡/低位试错半仓、主跌两成）。"""
    days = db.trade_days(start, end)
    if len(days) < 3:
        return {"error": "区间内交易日太少，请检查日期或先更新数据"}
    phases = emotion.phase_map()
    if (gate or phase_cap or strategy != "chan") and not phases:
        return {"error": "还没有情绪周期数据，请先在「数据」页更新（或点“只重算信号”）"}
    modes = modes or ["龙头", "补涨", "切换"]
    buy_by_day = defaultdict(list)
    names = {}
    gate_blocked = 0
    with db.session() as c:
        if strategy in ("chan", "both"):
            tsql, targs = _type_filter_sql(types)
            buys = c.execute(
                "SELECT s.code, s.detect_date, s.score, s.types, s.bsp_price, COALESCE(kd.is_st, st.is_st), st.name, "
                "e.phase, e.theme, e.theme_old, e.hyped, e.maxb5 FROM signals s JOIN stocks st ON st.code=s.code "
                "LEFT JOIN kline_d kd ON kd.code=s.code AND kd.date=s.detect_date "
                "LEFT JOIN sig_emo e ON e.code=s.code AND e.date=s.detect_date "
                "WHERE s.is_buy=1 AND s.detect_date>=? AND s.detect_date<=? AND s.score>=?" + tsql +
                " ORDER BY s.detect_date, s.score DESC", [start, end, min_score] + targs).fetchall()
            for code, d, score, types_, bsp_price, is_st, name, ph, theme, th_old, hyped, mb5 in buys:
                if exclude_st and is_st:
                    continue
                if boards and board_of(code) not in boards:
                    continue
                if gate and not gate_check(ph, th_old, hyped, mb5)[0]:
                    gate_blocked += 1
                    continue
                buy_by_day[d].append({"code": code, "score": score, "label": types_, "bsp": bsp_price,
                                      "mode": "缠论", "theme": theme})
                names[code] = name
        emo_by_day = defaultdict(list)
        theme_zt = {}
        if strategy in ("emotion", "both"):
            picks = c.execute(
                "SELECT p.code, p.date, p.score, p.mode, p.theme, p.detail, p.fit, COALESCE(kd.is_st, st.is_st), "
                "st.name FROM emo_picks p JOIN stocks st ON st.code=p.code "
                "LEFT JOIN kline_d kd ON kd.code=p.code AND kd.date=p.date "
                "WHERE p.date>=? AND p.date<=? AND p.score>=? ORDER BY p.date, p.score DESC",
                (start, end, emo_min_score)).fetchall()
            for code, d, score, mode, theme, det, fit, is_st, name in picks:
                if mode not in modes or (emo_fit_only and not fit) or (exclude_st and is_st):
                    continue
                if boards and board_of(code) not in boards:
                    continue
                det = json.loads(det)
                emo_by_day[d].append({"code": code, "score": score, "label": mode, "bsp": None, "mode": mode,
                                      "theme": theme, "leader": det.get("题材龙头")})
                names[code] = name
            for d, th, z in c.execute("SELECT date, theme, zt FROM emo_theme WHERE date>=? AND date<=?",
                                      (start, end)):
                theme_zt[(d, th)] = z
        sells = c.execute("SELECT code, detect_date FROM signals WHERE is_buy=0 AND detect_date>=? "
                          "AND detect_date<=?", (start, end)).fetchall()
    for d, lst in emo_by_day.items():       # 情绪选股排在缠论信号前面
        seen = {x["code"] for x in lst}
        buy_by_day[d] = lst + [x for x in buy_by_day.get(d, []) if x["code"] not in seen]
    sell_set = set(sells)

    px = _Prices(days[0], days[-1])
    cash = float(capital)
    held = {}
    pending_buy, buy_limit = [], max_pos
    pending_sell = {}
    trades, equity, npos, phase_log = [], [], [], []
    fee_buy = COMMISSION + SLIPPAGE
    fee_sell = COMMISSION + STAMP_TAX + SLIPPAGE

    def sell(code, day, price, reason):
        nonlocal cash
        p = held.pop(code)
        proceeds = p["shares"] * price * (1 - fee_sell)
        cash += proceeds
        trades.append({"code": code, "name": names.get(code, ""), "types": p["label"], "mode": p["mode"],
                       "theme": p["theme"], "score": p["score"], "buy_date": p["date"],
                       "buy_price": round(p["price"], 3), "sell_date": day, "sell_price": round(price, 3),
                       "days": p["hold"], "reason": reason, "ret": proceeds / p["cost"] - 1,
                       "phase": p["phase"]})

    for k, day in enumerate(days):
        ph = phases.get(day, (None, None))[0]
        # 1) 开盘：执行昨天决定的卖出（一字跌停/停牌卖不出，顺延）
        for code in list(pending_sell):
            if code not in held:
                pending_sell.pop(code)
            elif px.can_sell(code, day):
                sell(code, day, px.bar(code, day)[0], pending_sell.pop(code))
        # 2) 开盘：执行昨天决定的买入
        if pending_buy:
            eq_now = cash + sum(h["shares"] * h["last"] for h in held.values())
            alloc = eq_now / max_pos
            for cd in pending_buy:
                code = cd["code"]
                if len(held) >= buy_limit or code in held:
                    continue
                if not px.can_buy(code, day):
                    continue
                price = px.bar(code, day)[0]
                shares = int(min(alloc, cash) / (price * (1 + fee_buy)) / 100) * 100
                if shares <= 0:
                    continue
                cost = shares * price * (1 + fee_buy)
                cash -= cost
                stop = price * (1 - stop_pct) if stop_pct else 0
                if cd["mode"] == "缠论" and use_bsp_stop and cd["bsp"]:
                    stop = max(stop, cd["bsp"] * 0.99)
                mh = max_hold if cd["mode"] == "缠论" else min(x for x in (max_hold, MODE_MAX_HOLD[cd["mode"]]) if x)
                held[code] = {"date": day, "price": price, "shares": shares, "cost": cost, "stop": stop,
                              "tp": price * (1 + take_profit) if take_profit else None, "hold": 0, "last": price,
                              "score": cd["score"], "label": cd["label"], "mode": cd["mode"], "theme": cd["theme"],
                              "leader": cd.get("leader"), "max_hold": mh, "zt_prev": True,
                              "phase": cd.get("phase")}
            pending_buy = []
        # 3) 盘中：止损 / 止盈 / 到期（T+1：当天买的不能卖）
        for code in list(held):
            p = held[code]
            b = px.bar(code, day)
            if not b:
                continue
            o, h, low, c = b[:4]
            if p["date"] == day:
                p["last"] = c
                continue
            p["hold"] += 1
            if code in pending_sell or not px.can_sell(code, day):
                p["last"] = c
                continue
            if p["stop"] and low <= p["stop"]:
                sell(code, day, min(o, p["stop"]), "止损")
            elif p["tp"] and h >= p["tp"]:
                sell(code, day, max(o, p["tp"]), "止盈")
            elif p["max_hold"] and p["hold"] >= p["max_hold"]:
                sell(code, day, c, "到期")
            else:
                p["last"] = c
        # 4) 收盘：记账
        eq = cash + sum(h["shares"] * h["last"] for h in held.values())
        equity.append((day, eq))
        npos.append(len(held))
        phase_log.append(ph)
        # 5) 收盘后：决定明天的卖出
        for code, p in held.items():
            if code in pending_sell:
                continue
            mode = p["mode"]
            if mode == "缠论":
                if exit_on_sell and (code, day) in sell_set:
                    pending_sell[code] = "出现卖点"
                continue
            if not px.bar(code, day):
                continue
            zt = px.is_zt(code, day)
            pct = px.pct(code, day) or 0
            if ph == "主跌" and mode in ("龙头", "补涨"):
                pending_sell[code] = "进入主跌"
            elif mode == "龙头" and not zt and (pct < 0 or not p["zt_prev"]):
                pending_sell[code] = "龙头断板"
            elif mode == "补涨" and not zt:
                pending_sell[code] = "补涨不板就走"
            elif mode == "补涨" and p["leader"] and (px.pct(p["leader"].split()[-1], day) or 0) <= -0.05:
                pending_sell[code] = "龙头亏钱"
            elif mode == "切换" and p["hold"] >= 2 and p["theme"] and sum(
                    theme_zt.get((d, p["theme"]), 0) for d in days[max(0, k - 2):k + 1]) == 0:
                pending_sell[code] = "题材退潮"
            p["zt_prev"] = zt
        # 6) 按阶段降仓
        slots = phase_slots(max_pos, ph) if phase_cap else max_pos
        keep = [c_ for c_ in held if c_ not in pending_sell]
        if len(keep) > slots:
            keep.sort(key=lambda c_: held[c_]["last"] / held[c_]["price"])
            for c_ in keep[:len(keep) - slots]:
                pending_sell[c_] = f"阶段降仓（{ph}）"
        # 7) 决定明天的买入
        free = slots - (len(held) - len(pending_sell))
        if free > 0 and k + 1 < len(days):
            cands = [dict(x, phase=ph) for x in buy_by_day.get(day, []) if x["code"] not in held]
            pending_buy = cands[: free * 2]   # 多备几只，防止涨停买不进
            buy_limit = slots

    open_positions = [{"code": c_, "name": names.get(c_, ""), "mode": p["mode"], "buy_date": p["date"],
                       "buy_price": p["price"], "last": p["last"],
                       "ret": p["shares"] * p["last"] * (1 - fee_sell) / p["cost"] - 1}
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
        "gate_blocked": gate_blocked,
    }
    if len(tr):
        wins = tr.loc[tr["ret"] > 0, "ret"].sum()
        losses = -tr.loc[tr["ret"] <= 0, "ret"].sum()
        stats["profit_factor"] = float(wins / losses) if losses > 0 else None
        stats["by_reason"] = tr.groupby("reason")["ret"].agg(["count", "mean"]).reset_index().to_dict("records")
        g = tr.groupby("mode")["ret"]
        stats["by_mode"] = [{"mode": m, "count": int(len(s)), "mean": float(s.mean()), "win": float((s > 0).mean())}
                            for m, s in g]
    stats = {k: _clean(v) if not isinstance(v, list) else v for k, v in stats.items()}
    return {
        "stats": stats,
        "curve": {"dates": list(eq.index), "equity": [round(x, 2) for x in eq],
                  "bench": [None if pd.isna(x) else round(x, 2) for x in bench],
                  "drawdown": [round(x, 4) for x in dd], "phase": phase_log},
        "trades": [{k: _clean(v) for k, v in t.items()} for t in trades[::-1]],
        "open_positions": open_positions,
    }
