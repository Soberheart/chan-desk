"""连板天梯与次日晋级概率。

每天收盘后，把所有涨停股按连板高度排成天梯，并估计每只票“明天继续涨停（晋级）”的概率。
重点是二进三（二板 → 三板）。

概率来自逻辑回归，分首板 / 二板 / 三板+ 三个模型，特征分四组：
  情绪周期  水温、所处阶段、今天这一级的晋级率、昨日涨停溢价、炸板率、空间高度、同级竞争数、跌停家数
  题材      所属题材今日涨停数和增减、5日热度排名、是否主线 / 新题材 / 老题材、是不是题材内最高板、题材连板股数
  个股形态  一字 / T字、开盘涨幅、盘中回撤、换手和放大倍数、成交额、流通市值、启动前 20/60 日涨幅、
            是否突破 120 日新高、均线多头、近 20 日其他涨停次数、20% 涨跌幅板块；缠论近期买卖点
  分时封板  首封时间、开板次数、是否尾盘封（有分时数据时才用）

为了不偷看未来，模型按季度滚动训练：每个季度的概率只用这个季度之前的数据训练出来。最新一天用全部
已知结果训练的模型。样本外表现（校准、AUC、每天概率最高那只的晋级率、打板收益）存在 meta 里，页面上可看。
"""
import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from . import db
from .settings import COMMISSION, EMO, SIGNAL_START, SLIPPAGE, STAMP_TAX

SEGS = ("首板", "二板", "三板+")


def seg_of(b):
    return "首板" if b == 1 else ("二板" if b == 2 else "三板+")


# key, 中文名, 显示格式
FEATS = [
    ("temp", "水温", "n0"), ("ph_up", "主升期", "b"), ("ph_high", "高位震荡期", "b"), ("ph_down", "主跌期", "b"),
    ("promo_next", "今天这一级的晋级率", "r"), ("prev_zt_ret", "昨日涨停溢价", "p"), ("zb_rate", "炸板率", "r"),
    ("height", "空间高度", "n0"), ("same_cnt", "同级竞争数", "n0"), ("dt_n", "跌停家数", "n0"),
    ("th_zt", "题材今日涨停", "n0"), ("th_chg", "题材涨停增减", "n0"), ("th_rank", "题材5日热度排名", "rank"),
    ("th_main", "主线题材", "b"), ("th_new", "新题材", "b"), ("th_old", "老题材", "b"),
    ("th_leader", "题材内最高板", "b"), ("th_lb", "题材连板股数", "n0"),
    ("yizi", "一字板", "b"), ("tzi", "T字板", "b"), ("open_gap", "开盘涨幅", "p"), ("dip", "盘中最低", "p"),
    ("turn", "换手率", "pct"), ("turn_ratio", "换手放大倍数", "n1"), ("amt", "成交额(亿)", "e8"),
    ("mcap", "流通市值(亿)", "e8"), ("gain20", "启动前20日涨幅", "p"), ("gain60", "启动前60日涨幅", "p"),
    ("breakout", "相对120日高点", "p"), ("ma_bull", "均线多头排列", "b"), ("zt20", "近20日其他涨停", "n0"),
    ("cy", "20%涨跌幅", "b"),
    ("chan_buy", "近10日缠论买点", "b"), ("chan_b3", "近20日缠论三买", "b"), ("chan_sell", "近5日缠论卖点", "b"),
    ("board", "连板高度", "n0"), ("is_space", "空间板", "b"),
]
INTRA = [("seal_min", "首封时间", "seal"), ("open_times", "开板次数", "n0"), ("late", "尾盘才封", "b")]
LOG_FEATS = {"same_cnt", "dt_n", "th_zt", "th_lb", "amt", "mcap", "turn", "turn_ratio", "zt20"}
SEG_DROP = {"首板": {"board", "is_space"}, "二板": {"board", "is_space"}, "三板+": set()}
ALL = FEATS + INTRA
FIDX = {k: i for i, (k, _, _) in enumerate(ALL)}
MIN_TRAIN = 300
INTRA_COVER = 0.6


# ---------------------------------------------------------------- 工具
def _seal_minutes(hm):
    if not hm:
        return np.nan
    h, m = int(hm[:2]), int(hm[3:5])
    x = h * 60 + m
    if x <= 9 * 60 + 30:
        return 0.0
    if x <= 11 * 60 + 30:
        return float(x - (9 * 60 + 30))
    return float(120 + max(0, x - 13 * 60))


def fmt_val(kind, v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if kind == "b":
        return "是" if v >= 0.5 else "否"
    if kind == "p":
        return f"{v * 100:+.1f}%"
    if kind == "pct":
        return f"{v:.1f}%"
    if kind == "r":
        return f"{v * 100:.0f}%"
    if kind == "n0":
        return f"{v:.0f}"
    if kind == "n1":
        return f"{v:.1f}"
    if kind == "e8":
        return f"{v / 1e8:.1f}"
    if kind == "rank":
        return "-" if v >= 999 else f"第{v:.0f}"
    if kind == "seal":
        if v <= 0:
            return "竞价"
        x = v + 9 * 60 + 30 if v <= 120 else v - 120 + 13 * 60
        return f"{int(x // 60):02d}:{int(x % 60):02d}"
    return f"{v:.2f}"


def _transform(X):
    """对计数、金额类特征取 log1p，让分布别太偏。"""
    Z = X.copy()
    for k in LOG_FEATS:
        i = FIDX[k]
        Z[:, i] = np.log1p(np.clip(Z[:, i], 0, None))
    i = FIDX["th_rank"]
    Z[:, i] = 1.0 / np.clip(Z[:, i], 1, None)     # 排名 → 1/排名，没排名的接近 0
    return Z


def fit_logit(X, y, l2=2.0, iters=30):
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0, med)
    Xf = np.where(np.isnan(X), med, X)
    mu, sd = Xf.mean(0), Xf.std(0)
    keep = sd > 1e-9
    Z = (Xf[:, keep] - mu[keep]) / sd[keep]
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    w = np.zeros(Z.shape[1])
    w[0] = math.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6))
    R = np.eye(Z.shape[1]) * l2
    R[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + R @ w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + R
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-7:
            break
    return {"med": med, "mu": mu, "sd": sd, "keep": keep, "w": w}


def predict(m, X):
    Xf = np.where(np.isnan(X), m["med"], X)
    Z = (Xf[:, m["keep"]] - m["mu"][m["keep"]]) / m["sd"][m["keep"]]
    contrib = Z * m["w"][1:]
    logit = m["w"][0] + contrib.sum(1)
    return 1 / (1 + np.exp(-np.clip(logit, -30, 30))), contrib


def auc(p, y):
    y = np.asarray(y, bool)
    n1, n0 = y.sum(), (~y).sum()
    if n1 == 0 or n0 == 0:
        return None
    r = pd.Series(p).rank().to_numpy()
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


# ---------------------------------------------------------------- 主计算
def seal_pairs(ctx, since=None, last_days=None, min_board=1):
    days, codes, board = ctx["P"]["days"], ctx["P"]["codes"], ctx["board"]
    ztm = ctx["ztm"]
    T = len(days)
    t0 = T - last_days if last_days else next((i for i, d in enumerate(days) if d >= (since or "")), T)
    out = []
    for t in range(max(t0, 0), T):
        for j in np.nonzero(ztm[t] & (board[t] >= min_board))[0]:
            out.append((codes[j], days[t]))
    return out


def build(ctx, progress=lambda *a, **k: None):
    progress("stage", "连板天梯：计算特征")
    P = ctx["P"]
    days, codes = P["days"], P["codes"]
    T, N = len(days), len(codes)
    di = P["di"]
    ztm, dtm, board, valid = ctx["ztm"], ctx["dtm"], ctx["board"], ctx["valid"]
    pct, close, high, low, opn, amt, turn, cf = (P["pct"], P["close"], P["high"], P["low"], P["open"],
                                                 P["amount"], P["turn"], P["cf"])
    lim, phase, temp, height = ctx["lim"], ctx["phase"], ctx["temp"], ctx["height"]
    K, M, theme_zt, rank5 = ctx["K"], ctx["M"], ctx["theme_zt"], ctx["rank5"]
    tnames, attr, leaders = ctx["tnames"], ctx["attr"], ctx["leaders"]
    tol = EMO["zt_tol"]
    t_start = next((i for i, d in enumerate(days) if d >= SIGNAL_START), 0)
    t_start = max(t_start, 60)

    pb = np.vstack([np.zeros((1, N), board.dtype), board[:-1]])
    # 今天各级的晋级率：昨天是 b 板、今天继续涨停的比例（b=5 表示 5 板及以上）
    vn = valid & ctx["nst"]
    promo = np.full((T, 6), np.nan)
    for lv in range(1, 6):
        m_ = ((pb >= lv) if lv == 5 else (pb == lv)) & vn
        n = m_.sum(1)
        promo[:, lv] = np.where(n > 0, (m_ & ztm).sum(1) / np.maximum(n, 1), np.nan)
    cz = np.vstack([np.zeros((1, N), np.int32), np.cumsum(ztm, 0, dtype=np.int32)])   # cz[t+1] = 截至 t 的涨停次数
    th_lb = (((board >= 2) & ztm).astype(np.float32) @ M) if K else None
    ma = {n: pd.DataFrame(cf).rolling(n, min_periods=n).mean().to_numpy(np.float32) for n in (5, 10, 20)}
    tm = pd.DataFrame(turn).shift(1).rolling(5, min_periods=3).mean().to_numpy(np.float32)
    hi120 = pd.DataFrame(high).rolling(120, min_periods=40).max().to_numpy(np.float32)
    # 跌停连续天数
    dboard = np.zeros((T, N), np.int16)
    prev = np.zeros(N, np.int16)
    for t in range(T):
        prev = np.where(dtm[t], prev + 1, np.where(valid[t], 0, prev)).astype(np.int16)
        dboard[t] = prev

    # 缠论信号（按发现日）
    cix = {c: j for j, c in enumerate(codes)}
    buy_d, b3_d, sell_d = defaultdict(list), defaultdict(list), defaultdict(list)
    with db.session() as c:
        for code, d, is_buy, types in c.execute("SELECT code, detect_date, is_buy, types FROM signals"):
            j, t = cix.get(code), di.get(d)
            if j is None or t is None:
                continue
            if is_buy:
                buy_d[j].append(t)
                if "3" in (types or ""):
                    b3_d[j].append(t)
            else:
                sell_d[j].append(t)
        seals = {(a, b): (f, l_, o) for a, b, f, l_, o in c.execute("SELECT code, date, first_seal, last_seal, "
                                                                     "open_times FROM zt_seal")}

    def recent(lst, j, t, n):
        return float(any(t - n < x <= t for x in lst.get(j, ())))

    rows, meta = [], []
    for t in range(t_start, T):
        if np.isnan(temp[t]) or phase[t] is None:
            continue
        ph = phase[t]
        same = {}
        for j in np.nonzero(ztm[t])[0]:
            b = int(board[t, j])
            lv = min(b, 5)
            same.setdefault(lv, int((np.minimum(board[t], 5) == lv).sum()))
            a = attr(t, j) if K else None
            pc = close[t, j] / (1 + pct[t, j]) if pct[t, j] > -1 else np.nan
            o_, h_, l_, c_ = opn[t, j], high[t, j], low[t, j], close[t, j]
            yizi = float(o_ >= h_ * (1 - 1e-6) and l_ >= h_ * (1 - 1e-6))
            tzi = float(not yizi and o_ >= h_ * (1 - 1e-6) and l_ < h_ * (1 - 1e-4))
            s0 = t - b                                   # 本轮连板启动前一天
            g20 = cf[s0, j] / cf[s0 - 20, j] - 1 if s0 - 20 >= 0 and cf[s0 - 20, j] > 0 else np.nan
            g60 = cf[s0, j] / cf[s0 - 60, j] - 1 if s0 - 60 >= 0 and cf[s0 - 60, j] > 0 else np.nan
            brk = c_ / hi120[s0, j] - 1 if s0 >= 0 and hi120[s0, j] > 0 else np.nan
            zt20 = cz[s0 + 1, j] - cz[max(s0 - 19, 0), j] if s0 >= 0 else 0
            mb = float(ma[5][t, j] > ma[10][t, j] > ma[20][t, j]) if not np.isnan(ma[20][t, j]) else np.nan
            tv = turn[t, j]
            se = seals.get((codes[j], days[t]))
            x = {
                "temp": temp[t], "ph_up": float(ph == "主升"), "ph_high": float(ph == "高位震荡"),
                "ph_down": float(ph == "主跌"), "promo_next": promo[t, lv], "prev_zt_ret": ctx["prev_zt_ret"][t],
                "zb_rate": ctx["zb_rate"][t], "height": float(height[t]), "same_cnt": float(same[lv]),
                "dt_n": float(ctx["dt_n"][t]),
                "th_zt": float(theme_zt[t, a]) if a is not None else 0.0,
                "th_chg": float(theme_zt[t, a] - theme_zt[t - 1, a]) if a is not None else 0.0,
                "th_rank": float(rank5[t, a]) if a is not None else 9999.0,
                "th_main": float(ctx["main_mat"][t, a]) if a is not None else 0.0,
                "th_new": float(ctx["newt"][t, a]) if a is not None else 0.0,
                "th_old": float(ctx["old"][t, a]) if a is not None else 0.0,
                "th_leader": float(a is not None and leaders.get((t, a), (None, -1))[1] == j),
                "th_lb": float(th_lb[t, a]) if a is not None else 0.0,
                "yizi": yizi, "tzi": tzi, "open_gap": o_ / pc - 1 if pc > 0 else np.nan,
                "dip": l_ / pc - 1 if pc > 0 else np.nan,
                "turn": tv, "turn_ratio": tv / tm[t, j] if tm[t, j] and tm[t, j] > 0 else np.nan,
                "amt": amt[t, j], "mcap": amt[t, j] / (tv / 100) if tv and tv > 0 else np.nan,
                "gain20": g20, "gain60": g60, "breakout": brk, "ma_bull": mb, "zt20": float(zt20),
                "cy": float(lim[t, j] >= 0.2 - 1e-6),
                "chan_buy": recent(buy_d, j, t, 10), "chan_b3": recent(b3_d, j, t, 20),
                "chan_sell": recent(sell_d, j, t, 5),
                "board": float(b), "is_space": float(b == height[t]),
                "seal_min": _seal_minutes(se[0]) if se and se[0] else np.nan,
                "open_times": float(se[2]) if se and se[2] is not None else np.nan,
                "late": float((se[1] or "") >= "14:30") if se and se[1] else np.nan,
            }
            y = nxt_pct = dab = None
            if t + 1 < T and valid[t + 1, j]:
                y = int(ztm[t + 1, j])
                nxt_pct = float(pct[t + 1, j])
                touch = high[t + 1, j] / c_ - 1 >= lim[t + 1, j] - tol
                yz1 = opn[t + 1, j] >= high[t + 1, j] * (1 - 1e-6) and low[t + 1, j] >= high[t + 1, j] * (1 - 1e-6)
                if touch and not yz1 and t + 2 < T and valid[t + 2, j]:
                    dab = float(opn[t + 2, j] / high[t + 1, j] - 1 - (2 * COMMISSION + STAMP_TAX + 2 * SLIPPAGE))
            # N天M板
            w0 = max(t - 19, 0)
            zz = np.nonzero(ztm[w0:t + 1, j])[0]
            m_cnt = len(zz)
            nm = f"{t - (w0 + zz[0]) + 1}天{m_cnt}板" if m_cnt > b else None
            tags = {"yizi": bool(yizi), "tzi": bool(tzi), "cy": bool(x["cy"]), "nm": nm,
                    "first": se[0] if se else None, "last": se[1] if se else None,
                    "opens": se[2] if se else None, "turn": None if np.isnan(tv) else round(float(tv), 2)}
            rows.append([x[k] for k, _, _ in ALL])
            meta.append((t, j, b, tnames[a] if a is not None else None, tags, y, nxt_pct, dab))

    X = np.array(rows, dtype=float) if rows else np.zeros((0, len(ALL)))
    Xt = _transform(X) if len(X) else X
    seg = np.array([seg_of(m[2]) for m in meta])
    yv = np.array([np.nan if m[5] is None else m[5] for m in meta], float)
    tt = np.array([m[0] for m in meta], int)
    probs = np.full(len(meta), np.nan)
    contribs = [None] * len(meta)
    ev = {}
    models = {}

    progress("stage", "连板天梯：滚动训练晋级概率模型")
    quarter = np.array([days[t][:4] + "Q" + str((int(days[t][5:7]) - 1) // 3 + 1) for t in tt]) if len(tt) else tt
    for sname in SEGS:
        idx = np.nonzero(seg == sname)[0]
        if not len(idx):
            continue
        cols = [i for i, (k, _, _) in enumerate(ALL) if k not in SEG_DROP[sname]]
        intra_cols = [FIDX[k] for k, _, _ in INTRA]
        known = idx[~np.isnan(yv[idx])]
        cover = float((~np.isnan(X[known, FIDX["seal_min"]])).mean()) if len(known) else 0.0
        use_intra = cover >= INTRA_COVER
        if not use_intra:
            cols = [i for i in cols if i not in intra_cols]
        qs = sorted(set(quarter[idx]))
        for q in qs:
            tr = known[quarter[known] < q]
            te = idx[quarter[idx] == q]
            if len(tr) < MIN_TRAIN or tr.size and np.unique(yv[tr]).size < 2:
                continue
            tr = tr[tt[tr] >= tt[tr].max() - 750]          # 最近约三年
            m = fit_logit(Xt[np.ix_(tr, cols)], yv[tr])
            p, cb = predict(m, Xt[np.ix_(te, cols)])
            probs[te] = p
            for r_, i in enumerate(te):
                contribs[i] = (cols, m, cb[r_])
        # 最新一天：用全部已知结果训练
        last_t = T - 1
        live = idx[tt[idx] == last_t]
        if len(known) >= MIN_TRAIN and np.unique(yv[known]).size == 2:
            tr = known[tt[known] >= tt[known].max() - 750]
            m = fit_logit(Xt[np.ix_(tr, cols)], yv[tr])
            models[sname] = (cols, m)
            if len(live):
                p, cb = predict(m, Xt[np.ix_(live, cols)])
                probs[live] = p
                for r_, i in enumerate(live):
                    contribs[i] = (cols, m, cb[r_])
        ev[sname] = evaluate(idx, probs, yv, tt, meta, cover, use_intra)
        if sname in models:
            cols_, m = models[sname]
            ws = m["w"][1:]
            kept = [c_ for c_, k in zip(cols_, m["keep"]) if k]
            ev[sname]["coef"] = sorted([{"label": ALL[c_][1], "w": round(float(w_), 3)} for c_, w_ in zip(kept, ws)],
                                       key=lambda d: -abs(d["w"]))

    # ------------------------------------------------ 写库
    progress("stage", "连板天梯：写入结果")
    out = []
    for i, (t, j, b, th, tags, y, nxp, dab) in enumerate(meta):
        why = None
        if contribs[i] is not None:
            cols, m, cb = contribs[i]
            kept = [c_ for c_, k in zip(cols, m["keep"]) if k]
            items = sorted(zip(kept, cb), key=lambda z: -abs(z[1]))[:6]
            why = json.dumps([[ALL[c_][1], fmt_val(ALL[c_][2], X[i, c_]), round(float(v), 2)] for c_, v in items],
                             ensure_ascii=False)
        out.append((days[t], codes[j], "zt", b, th, json.dumps(tags, ensure_ascii=False),
                    None if np.isnan(probs[i]) else round(float(probs[i]), 4), why, y,
                    None if nxp is None else round(nxp, 4), None if dab is None else round(dab, 4)))
    for t in range(t_start, T):
        for j in np.nonzero(dtm[t])[0]:
            a = attr(t, j) if K else None
            out.append((days[t], codes[j], "dt", int(dboard[t, j]), tnames[a] if a is not None else None,
                        json.dumps({"cy": bool(lim[t, j] >= 0.2 - 1e-6)}), None, None, None, None, None))
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM ladder")
        conn.executemany("INSERT OR REPLACE INTO ladder VALUES(?,?,?,?,?,?,?,?,?,?,?)", out)
        conn.commit()
    finally:
        conn.close()
    db.set_meta("ladder_eval", ev)
    db.set_meta("last_ladder_build", {"time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"), "rows": len(out)})
    return len(out)


def evaluate(idx, probs, yv, tt, meta, cover, use_intra):
    sel = idx[~np.isnan(probs[idx]) & ~np.isnan(yv[idx])]
    res = {"n": int(len(sel)), "intra_cover": round(cover, 3), "use_intra": use_intra}
    if len(sel) < 50:
        return res
    p, y = probs[sel], yv[sel]
    res["base"] = float(y.mean())
    res["auc"] = auc(p, y)
    res["brier"] = float(((p - y) ** 2).mean())
    q = pd.qcut(pd.Series(p).rank(method="first"), 10, labels=False)
    cal = []
    for k in range(10):
        m_ = (q == k).to_numpy()
        cal.append({"p": float(p[m_].mean()), "rate": float(y[m_].mean()), "n": int(m_.sum())})
    res["calib"] = cal
    # 每天概率最高的 1 只 / 前 3 只
    df = pd.DataFrame({"t": tt[sel], "p": p, "y": y,
                       "dab": [meta[i][7] for i in sel]})
    df = df.sort_values(["t", "p"], ascending=[True, False])
    g = df.groupby("t")
    top1 = g.head(1)
    top3 = g.head(3)
    res["top1"] = {"rate": float(top1["y"].mean()), "days": int(len(top1)),
                   "dab": _clean_mean(top1["dab"]), "dab_n": int(top1["dab"].notna().sum())}
    res["top3"] = {"rate": float(top3["y"].mean()), "n": int(len(top3)), "dab": _clean_mean(top3["dab"])}
    # 打板：次日盘中摸到涨停价就按涨停价买，后天开盘卖（含炸板亏损）
    qb = pd.qcut(pd.Series(p).rank(method="first"), 5, labels=False).to_numpy()
    dab_rows = []
    for k in range(5):
        m_ = qb == k
        d_ = pd.Series([meta[i][7] for i in sel], dtype=float)[m_]
        dab_rows.append({"p_lo": float(p[m_].min()), "p_hi": float(p[m_].max()), "rate": float(y[m_].mean()),
                         "n": int(m_.sum()), "trades": int(d_.notna().sum()), "dab": _clean_mean(d_),
                         "dab_win": _clean_mean((d_.dropna() > 0).astype(float))})
    res["dab"] = dab_rows
    return res


def _clean_mean(s):
    s = pd.Series(s, dtype=float).dropna()
    return float(s.mean()) if len(s) else None


# ---------------------------------------------------------------- 查询
def day(date=None, kind="zt"):
    with db.session() as c:
        dates = [r[0] for r in c.execute("SELECT DISTINCT date FROM ladder ORDER BY date")]
        if not dates:
            return None
        if not date or date not in dates:
            cand = [d for d in dates if not date or d <= date]
            date = cand[-1] if cand else dates[0]
        i = dates.index(date)
        prev_d = dates[i - 1] if i > 0 else None
        names = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
        rows = c.execute("SELECT code, board, theme, tags, prob, why, nxt_zt, nxt_pct, dab_ret FROM ladder "
                         "WHERE date=? AND kind=?", (date, kind)).fetchall()
        failed = []
        if kind == "zt" and prev_d:
            failed = c.execute("SELECT code, board, theme, tags, prob, nxt_pct FROM ladder WHERE date=? AND kind='zt' "
                               "AND nxt_zt=0", (prev_d,)).fetchall()
        # 今天每一级的晋级率（昨天 b-1 板 → 今天 b 板）
        prev_cnt = defaultdict(int)
        if prev_d:
            for b, n in c.execute("SELECT board, COUNT(*) FROM ladder WHERE date=? AND kind='zt' AND nxt_zt IS NOT NULL "
                                  "GROUP BY board", (prev_d,)):
                prev_cnt[b] += n
        emo = c.execute("SELECT phase, temp FROM emo_daily WHERE date=?", (date,)).fetchone()
    out = []
    for code, b, th, tags, prob, why, ny, np_, dab in rows:
        out.append({"code": code, "name": names.get(code, ""), "board": b, "theme": th, "tags": json.loads(tags),
                    "prob": prob, "why": json.loads(why) if why else [], "nxt_zt": ny, "nxt_pct": np_, "dab": dab})
    fails = [{"code": code, "name": names.get(code, ""), "board": b + 1, "from": b, "theme": th,
              "tags": json.loads(tags), "prob": prob, "pct": np_} for code, b, th, tags, prob, np_ in failed]
    levels = {}
    for r in out:
        levels.setdefault(r["board"], 0)
        levels[r["board"]] += 1
    promo = {}
    for b in levels:
        base_n = prev_cnt.get(b - 1, 0) if b > 1 else 0
        if base_n:
            promo[b] = {"rate": levels[b] / base_n if b > 1 else None, "from": base_n}
    ev = db.get_meta("ladder_eval") or {}
    brief = {k: {kk: v.get(kk) for kk in ("n", "base", "auc", "top1", "top3", "use_intra", "intra_cover")}
             for k, v in ev.items()}
    return {"date": date, "prev": prev_d, "next": dates[i + 1] if i + 1 < len(dates) else None, "kind": kind,
            "rows": out, "failed": fails, "promo": promo,
            "emotion": {"phase": emo[0], "temp": emo[1]} if emo else None, "eval": brief}


def evaluation():
    return db.get_meta("ladder_eval") or {}
