"""情绪周期（按 92科比 的四阶段 / 三模式框架实现）。

一个周期分四个阶段：低位试错 → 主升 → 高位震荡 → 主跌 → 回到低位试错。
三种操作模式：做高位龙头（主升）、做低位补涨（主升、高位震荡）、做题材切换（主跌之后）。

这里把这套“盘感”翻译成可以每天收盘后计算、可以回测的规则：

  水温（0-100）  每天算 8 个情绪分项（昨日涨停溢价、连板晋级率、封板率、涨跌停比、大面数、
                 空间高度、上涨家数占比、高位股表现），每项取它在过去 250 个交易日里的分位数，
                 平均后再做 3 日平滑。用分位数而不是绝对值，是因为不同年份涨停家数差别很大。
  阶段           一个带“最少停留天数”的状态机，按水温高低、水温变化、空间高度、高位股是否被杀来切换。
  题材           每天按题材统计涨停数；近 5 日涨停数排名靠前的是主线；主升/高位震荡期做过主线的
                 题材在之后 40 天内都算“老题材”；当天新出现涨停、过去 40 天从没排进前 10、且和
                 老题材成分重合度低的，算“新题材”（92科比：新题材要和老亏钱题材逻辑不一样）。

所有指标只用当天及以前的行情（题材成分股是当前快照，见 themes.py 的说明）。
阈值都在 settings.EMO 里，规则是经验翻译，请用“历史验证”里的按阶段统计检验它是否有效。
"""
import json
import math

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import db, themes
from .limits import CHINEXT_REFORM
from .settings import EMO, MODE_PHASES, PHASE_CAP, PHASES

ADVICE = {
    "低位试错": "题材试错期，往往接在主跌之后。最好的机会是高低切：布局还没被炒作过、和老题材逻辑不同的新方向；"
            "龙头可以小仓位试错，感受周期。",
    "主升": "市场合力形成，赚钱效应全面扩散。核心是做高位龙头，龙头不断超预期、跟风走强，做高位性价比最高；"
          "不要老去做低位。补涨在龙头打出赚钱效应后才有。",
    "高位震荡": "高潮后的分歧期：有穿越龙，也有大量中位股亏钱。回避中位（4进5、5进6），不能满仓，要降仓位。"
            "接下来要么穿越继续打开高度，要么转入主跌。",
    "主跌": "高位票连续杀跌、中位股大面积A杀。不要随便抄底老题材，老题材的反弹大多是诱多。"
          "轻仓或空仓，等主跌结束、新题材出现。",
}
RECOMMEND = {"低位试错": ["切换", "龙头"], "主升": ["龙头", "补涨"], "高位震荡": ["龙头", "补涨"], "主跌": ["切换"]}
TEMP_PARTS = ["昨日涨停溢价", "连板晋级率", "封板率", "涨跌停比", "大面", "空间高度", "上涨家数", "高位股表现"]


# ---------------------------------------------------------------- 工具
def _rmax(a, w):
    pad = np.concatenate([np.zeros((w - 1, a.shape[1]), a.dtype), a])
    return sliding_window_view(pad, w, axis=0).max(axis=-1)


def _shift(a, fill=0):
    out = np.empty_like(a)
    out[0] = fill
    out[1:] = a[:-1]
    return out


def _rpct(x, w, m):
    """x[t] 在过去 w 天（含当天）里的分位数，0-1。"""
    def f(a):
        last = a[-1]
        if np.isnan(last):
            return np.nan
        b = a[~np.isnan(a)]
        if len(b) < 2:
            return np.nan
        return ((b < last).sum() + 0.5 * ((b == last).sum() - 1)) / (len(b) - 1)
    return pd.Series(np.asarray(x, float)).rolling(w, min_periods=m).apply(f, raw=True).to_numpy()


def _nanmean_rows(a):
    n = (~np.isnan(a)).sum(0)
    return np.where(n > 0, np.nansum(a, 0) / np.maximum(n, 1), np.nan)


def _nan(x):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x


def _r(x, d=4):
    x = _nan(float(x)) if x is not None else None
    return None if x is None else round(x, d)


# ---------------------------------------------------------------- 数据面板
def load_panel():
    days = db.trade_days()
    with db.session() as c:
        stocks = c.execute("SELECT code, ipo_date FROM stocks WHERE bars>0 ORDER BY code").fetchall()
    codes = [s[0] for s in stocks]
    T, N = len(days), len(codes)
    di = {d: i for i, d in enumerate(days)}
    F = {k: np.full((T, N), np.nan, np.float32) for k in ("open", "high", "low", "close", "amount", "pct")}
    st = np.zeros((T, N), bool)
    new = np.zeros((T, N), bool)
    conn = db.connect()
    try:
        for j, (code, ipo) in enumerate(stocks):
            rows = conn.execute("SELECT date, open, high, low, close, amount, pct_chg, is_st FROM kline_d "
                                "WHERE code=? ORDER BY date", (code,)).fetchall()
            if not rows:
                continue
            idx = np.array([di.get(r[0], -1) for r in rows])
            arr = np.array([r[1:] for r in rows], dtype=float)
            ok = idx >= 0
            idx, arr = idx[ok], arr[ok]
            for col, k in enumerate(("open", "high", "low", "close", "amount", "pct")):
                F[k][idx, j] = arr[:, col]
            st[idx, j] = np.nan_to_num(arr[:, 6]) > 0
            if len(idx) and ipo and days and ipo >= days[0]:
                new[idx[:EMO["new_stock_days"]], j] = True
    finally:
        conn.close()
    close = F["close"]
    cf = pd.DataFrame(close).ffill().to_numpy(np.float32)
    prev = _shift(cf, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        fb = close / prev - 1
    miss = np.isnan(F["pct"]) & ~np.isnan(close)
    F["pct"][miss] = fb[miss]
    return {"days": days, "codes": codes, "di": di, "st": st, "new": new, "cf": cf, **F}


def limit_matrix(P):
    days, codes, st = P["days"], P["codes"], P["st"]
    lim = np.where(st, 0.05, 0.10).astype(np.float32)
    b = np.array([c[:5] for c in codes])
    after = np.array(days) >= CHINEXT_REFORM
    lim[:, b == "sh.68"] = 0.20
    lim[np.ix_(after, b == "sz.30")] = 0.20
    return lim


# ---------------------------------------------------------------- 主计算
def build(progress=lambda *a, **k: None):
    db.init_db()
    progress("stage", "情绪周期：读取全市场日线")
    P = load_panel()
    days, codes = P["days"], P["codes"]
    T, N = len(days), len(codes)
    if T < 30 or N == 0:
        return 0
    tol = EMO["zt_tol"]
    pct, close, high, low, opn, amt, cf = P["pct"], P["close"], P["high"], P["low"], P["open"], P["amount"], P["cf"]
    lim = limit_matrix(P)
    valid = ~np.isnan(close)
    base = valid & ~P["new"]
    nst = ~P["st"]
    with np.errstate(invalid="ignore", divide="ignore"):
        pc = close / (1 + pct)
        zt = base & (pct >= lim - tol) & (close >= high * (1 - 1e-6))
        touch = base & (high / pc - 1 >= lim - tol)
        dtm = base & nst & (pct <= -(lim - tol)) & (close <= low * (1 + 1e-6))
    ztm = zt & nst
    zbm = touch & ~zt & nst

    progress("stage", "情绪周期：计算连板、水温与阶段")
    board = np.zeros((T, N), np.int16)
    prevb = np.zeros(N, np.int16)
    for t in range(T):
        prevb = np.where(ztm[t], prevb + 1, np.where(valid[t], 0, prevb)).astype(np.int16)
        board[t] = prevb
    maxb5, maxb10, maxb40 = _rmax(board, 5), _rmax(board, 10), _rmax(board, 40)
    pb = _shift(board)
    pzt = _shift(ztm, False)
    pctf = np.nan_to_num(pct)
    vn = valid & nst

    def mmean(mask):
        n = mask.sum(1)
        s = np.where(mask, pctf, 0).sum(1)
        return np.where(n > 0, s / np.maximum(n, 1), np.nan)

    def frac(hit, mask):
        n = mask.sum(1)
        return np.where(n > 0, (hit & mask).sum(1) / np.maximum(n, 1), np.nan)

    zt_n, dt_n, zb_n = ztm.sum(1), dtm.sum(1), zbm.sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        zb_rate = np.where(zt_n + zb_n > 0, zb_n / (zt_n + zb_n), np.nan)
    height = board.max(1).astype(int)
    lb_n = (board >= 2).sum(1)
    promo1 = frac(ztm, (pb == 1) & vn)
    promo_lb = frac(ztm, (pb >= 2) & vn)
    prev_zt_ret = mmean(pzt & vn)
    prev_lb_ret = mmean((pb >= 2) & vn)
    damian = (pzt & vn & (pct <= -0.05)).sum(1)
    hp_ret = mmean((_shift(maxb10) >= EMO["high_board"]) & vn)
    lo_, hi_ = EMO["mid_board"]
    pm5 = _shift(maxb5)
    mid_ret = mmean((pm5 >= lo_) & (pm5 <= hi_) & vn)
    kill = ((_shift(maxb10) >= 3) & vn & (pct <= -0.07)).sum(1)
    up_ratio = frac(pct > 0, vn)
    amount = np.where(valid, np.nan_to_num(amt), 0).sum(1) / 1e8

    w, m = EMO["temp_window"], EMO["temp_min_periods"]
    promo = _nanmean_rows(np.vstack([promo1, promo_lb]))
    parts = {
        "昨日涨停溢价": prev_zt_ret, "连板晋级率": promo, "封板率": 1 - zb_rate,
        "涨跌停比": np.log((zt_n + 1) / (dt_n + 1)), "大面": -damian.astype(float), "空间高度": height.astype(float),
        "上涨家数": up_ratio, "高位股表现": hp_ret,
    }
    pr = {k: _rpct(v, w, m) for k, v in parts.items()}
    temp_raw = _nanmean_rows(np.vstack(list(pr.values()))) * 100
    temp = pd.Series(temp_raw).ewm(span=3, ignore_na=True).mean().to_numpy().copy()
    temp[np.isnan(temp_raw)] = np.nan
    kill_rank = _rpct(kill.astype(float), w, m)
    dt_rank = _rpct(dt_n.astype(float), w, m)
    phase, notes = run_phases(temp, height, hp_ret, kill_rank, dt_rank)

    # ------------------------------------------------ 题材
    progress("stage", "情绪周期：题材热度、新老题材")
    mem = themes.load_membership()
    tnames = sorted({t for ts in mem.values() for t in ts})
    tix = {t: i for i, t in enumerate(tnames)}
    K = len(tnames)
    mem_idx = [[tix[t] for t in mem.get(c, [])] for c in codes]
    M = np.zeros((N, max(K, 1)), np.float32)
    for j, ks in enumerate(mem_idx):
        M[j, ks] = 1
    size = M.sum(0)
    theme_zt = ztm.astype(np.float32) @ M
    heat = (board * ztm).astype(np.float32) @ M
    cs = np.cumsum(theme_zt, 0)
    zt5 = cs - np.vstack([np.zeros((5, M.shape[1]), np.float32), cs[:-5]])
    order = np.argsort(-zt5, axis=1, kind="stable")
    rank5 = np.empty_like(order)
    np.put_along_axis(rank5, order, np.arange(1, M.shape[1] + 1)[None, :].repeat(T, 0), axis=1)
    rank5 = np.where(zt5 > 0, rank5, 9999)
    main_mat = (rank5 <= EMO["main_rank"]) & (zt5 >= EMO["main_min_zt5"])
    hot_phase = np.array([p in ("主升", "高位震荡") for p in phase])
    L = EMO["old_lookback"]
    elig = np.cumsum(main_mat & hot_phase[:, None], 0)
    top_n = np.cumsum((rank5 <= EMO["new_rank"]), 0)

    def win_any(csum, t, a, b):   # 任意一天在 [t-a, t-b] 内
        hi_i, lo_i = t - b, t - a - 1
        if hi_i < 0:
            return np.zeros(csum.shape[1], bool)
        return (csum[hi_i] - (csum[lo_i] if lo_i >= 0 else 0)) > 0

    inter = M.T @ M
    J = inter / np.maximum(size[:, None] + size[None, :] - inter, 1)
    old = np.zeros((T, M.shape[1]), bool)
    newt = np.zeros((T, M.shape[1]), bool)
    maxj = np.zeros((T, M.shape[1]), np.float32)
    for t in range(T):
        if K == 0:
            break
        old[t] = win_any(elig, t, L, 1)
        if old[t].any():
            maxj[t] = J[:, old[t]].max(1)
        newt[t] = ((theme_zt[t] >= EMO["new_min_zt"]) & ~old[t] & ~win_any(top_n, t, L, 3)
                   & (maxj[t] < EMO["new_overlap"]))

    def attr(t, j):
        ks = mem_idx[j]
        if not ks:
            return None
        sc = [(theme_zt[t, k] * 1000 + zt5[t, k] * 10 - size[k] / 1000, k) for k in ks if zt5[t, k] > 0]
        return max(sc)[1] if sc else None

    # 题材龙头（当天涨停里板数最高、成交额最大）
    leaders = {}
    for t in range(T):
        for j in np.nonzero(ztm[t])[0]:
            key = (int(board[t, j]), float(np.nan_to_num(amt[t, j])))
            for k in mem_idx[j]:
                cur = leaders.get((t, k))
                if cur is None or key > cur[0]:
                    leaders[(t, k)] = (key, j)

    # ------------------------------------------------ 写库：每日
    progress("stage", "情绪周期：写入结果")
    daily_rows, theme_rows = [], []
    for t in range(T):
        lad = sorted(((int(board[t, j]), codes[j], tnames[a] if (a := attr(t, j)) is not None else None)
                      for j in np.nonzero(board[t] >= 2)[0]), reverse=True)[:80]
        ml = [tnames[k] for k in np.nonzero(main_mat[t])[0]] if K else []
        ol = [tnames[k] for k in np.nonzero(old[t])[0]] if K else []
        daily_rows.append((
            days[t], phase[t], _r(temp[t], 2), _r(temp_raw[t], 2), PHASE_CAP.get(phase[t]) if phase[t] else None,
            int(zt_n[t]), int(dt_n[t]), int(zb_n[t]), _r(zb_rate[t]), int(height[t]), int(lb_n[t]),
            _r(promo1[t]), _r(promo_lb[t]), _r(prev_zt_ret[t]), _r(prev_lb_ret[t]), int(damian[t]),
            _r(hp_ret[t]), _r(mid_ret[t]), int(kill[t]), _r(up_ratio[t]), _r(amount[t], 1),
            notes[t], json.dumps([[b, c, th] for b, c, th in lad], ensure_ascii=False),
            json.dumps(ml, ensure_ascii=False), json.dumps(ol, ensure_ascii=False)))
        if K:
            for k in np.nonzero(zt5[t] > 0)[0]:
                ld = leaders.get((t, k))
                theme_rows.append((days[t], tnames[k], int(theme_zt[t, k]), int(zt5[t, k]), int(heat[t, k]),
                                   int(rank5[t, k]), codes[ld[1]] if ld else None, ld[0][0] if ld else None,
                                   int(old[t, k]), int(newt[t, k])))

    # ------------------------------------------------ 情绪选股
    picks = []
    for t in range(T):
        ph = phase[t]
        if ph is None:
            continue
        zj = np.nonzero(ztm[t])[0]
        if not len(zj):
            continue
        a = np.nan_to_num(amt[t, zj])
        arank = {int(j): float(r) for j, r in zip(zj, pd.Series(a).rank(pct=True).to_numpy())}
        H = int(height[t])

        def gain(j, n):
            if t - n - 1 < 0 or np.isnan(cf[t - n - 1, j]) or cf[t - n - 1, j] <= 0:
                return None
            return float(cf[t - 1, j] / cf[t - n - 1, j] - 1)

        def add(mode, j, k, score, det, fit):
            picks.append((t, mode, int(j), tnames[k] if k is not None else None, int(board[t, j]),
                          round(float(score), 1), int(fit), det))

        # 龙头：热度前三题材的最高板 + 市场空间板
        got = set()
        if K:
            hot_k = [k for k in np.lexsort((-theme_zt[t], -heat[t])) if theme_zt[t, k] >= 2][:3]
            for i, k in enumerate(hot_k):
                ld = leaders.get((t, k))
                if ld and board[t, ld[1]] >= 2 and ld[1] not in got:
                    got.add(ld[1])
                    j = ld[1]
                    bonus = (30, 20, 10)[i]
                    b = int(board[t, j])
                    fit = ph in MODE_PHASES["龙头"] and (ph != "高位震荡" or b == H)
                    det = {"板数": b, "空间高度": H, "题材热度排名": i + 1, "成交额分位": round(arank[int(j)], 2)}
                    add("龙头", j, k, 40 * b / max(H, 1) + bonus + 20 * arank[int(j)] + (10 if fit else 0), det, fit)
        if H >= 3:
            for j in np.nonzero(board[t] == H)[0]:
                if j in got or not ztm[t, j]:
                    continue
                fit = ph in MODE_PHASES["龙头"]
                det = {"板数": H, "空间高度": H, "题材热度排名": None, "成交额分位": round(arank.get(int(j), 0), 2)}
                add("龙头", j, attr(t, j), 40 + 15 + 20 * arank.get(int(j), 0) + (10 if fit else 0), det, fit)

        if not K:
            continue
        # 补涨：主线题材（5日涨停前二）里龙头已打出高度、自身仍强，挖同题材低位首板/二板
        main_k = [k for k in np.argsort(rank5[t]) if rank5[t, k] <= 2]
        for i, k in enumerate(main_k):
            ld = leaders.get((t, k))
            if not ld:
                continue
            lj, lb = ld[1], ld[0][0]
            if lb < EMO["bz_leader_board"]:
                continue
            for j in zj:
                if j == lj or k not in mem_idx[j] or board[t, j] > 2:
                    continue
                g = gain(j, 20)
                if g is None or g >= EMO["bz_max_gain20"]:
                    continue
                fit = ph in MODE_PHASES["补涨"]
                sc = ((30, 20)[i] + 20 * min(1, lb / 6) + 25 * (1 - min(max(g, 0) / EMO["bz_max_gain20"], 1))
                      + (15 if board[t, j] == 1 else 8) + 10 * arank[int(j)])
                det = {"题材龙头": codes[lj], "龙头板数": int(lb), "20日涨幅": round(g, 4), "板数": int(board[t, j])}
                add("补涨", j, k, sc, det, fit)
        # 切换：新题材里的低位首板/二板
        nk = sorted(np.nonzero(newt[t])[0], key=lambda k: -theme_zt[t, k])[:3]
        for k in nk:
            cands = []
            for j in zj:
                if k not in mem_idx[j] or board[t, j] > 2:
                    continue
                g = gain(j, 60)
                if g is None or g >= EMO["qh_max_gain60"]:
                    continue
                cands.append((int(board[t, j]), arank[int(j)], j, g))
            for b, ar, j, g in sorted(cands, reverse=True)[:2]:
                fit = ph in MODE_PHASES["切换"]
                sc = (30 * min(1, theme_zt[t, k] / 5) + 25 * (1 - min(maxj[t, k] / EMO["new_overlap"], 1))
                      + (20 if b == 2 else 12) + 15 * (1 - min(max(g, 0) / EMO["qh_max_gain60"], 1)) + 10 * ar)
                det = {"题材涨停数": int(theme_zt[t, k]), "与老题材重合度": round(float(maxj[t, k]), 3),
                       "60日涨幅": round(g, 4), "板数": b}
                add("切换", j, k, sc, det, fit)

    pick_rows = []
    for t, mode, j, th, b, sc, fit, det in picks:
        o1 = opn[t + 1, j] if t + 1 < T else np.nan
        buyable = bool(t + 1 < T and not np.isnan(o1) and close[t, j] > 0
                       and o1 / close[t, j] - 1 < lim[t + 1, j] - EMO["zt_tol"])
        det["阶段"] = phase[t]

        def ofwd(h):
            if np.isnan(o1) or t + h >= T or o1 <= 0:
                return None
            return _r(cf[t + h, j] / o1 - 1)
        mdd = None
        if not np.isnan(o1) and o1 > 0:
            seg = low[t + 1:min(T, t + 6), j]
            seg = seg[~np.isnan(seg)]
            mdd = _r(seg.min() / o1 - 1) if len(seg) else None
        pick_rows.append((days[t], mode, codes[j], th, b, sc, fit, json.dumps(det, ensure_ascii=False),
                          int(buyable) if t + 1 < T else None, ofwd(1), ofwd(3), ofwd(5), ofwd(10), mdd))

    # ------------------------------------------------ 缠论信号的情绪标记
    with db.session() as c:
        sig = c.execute("SELECT DISTINCT code, detect_date FROM signals WHERE is_buy=1").fetchall()
    cix = {cd: j for j, cd in enumerate(codes)}
    sig_rows = []
    for code, d in sig:
        t, j = P["di"].get(d), cix.get(code)
        if t is None or j is None:
            continue
        a = attr(t, j)
        th_old = bool(maxb40[t, j] >= 1 and K and any(old[t, k] for k in mem_idx[j]))
        sig_rows.append((code, d, phase[t], tnames[a] if a is not None else None, int(th_old),
                         int(bool(a is not None and newt[t, a])), int(maxb40[t, j]), int(maxb5[t, j])))

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        for tb in ("emo_daily", "emo_theme", "emo_picks", "sig_emo"):
            conn.execute(f"DELETE FROM {tb}")
        conn.executemany("INSERT INTO emo_daily VALUES(" + ",".join(["?"] * 25) + ")", daily_rows)
        conn.executemany("INSERT INTO emo_theme VALUES(?,?,?,?,?,?,?,?,?,?)", theme_rows)
        conn.executemany("INSERT OR REPLACE INTO emo_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pick_rows)
        conn.executemany("INSERT OR REPLACE INTO sig_emo VALUES(?,?,?,?,?,?,?,?)", sig_rows)
        conn.commit()
    finally:
        conn.close()
    last = next((p for p in reversed(phase) if p), None)
    db.set_meta("last_emotion_build", {"time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"), "phase": last,
                                       "themes": K, "picks": len(pick_rows)})
    return len(pick_rows)


def run_phases(temp, height, hp_ret, kill_rank, dt_rank):
    T = len(temp)
    phase, notes = [None] * T, [""] * T
    cur, stay, peak = None, 0, 0
    for t in range(T):
        x = temp[t]
        if np.isnan(x):
            continue
        if cur is None:
            cur, stay = "低位试错", 0
        x3 = temp[t - 3] if t >= 3 and not np.isnan(temp[t - 3]) else x
        d3 = x - x3
        H = int(height[t])
        hp = hp_ret[t]
        kr = kill_rank[t]
        hot, cold = x >= EMO["hot"], x <= EMO["cold"]
        killed = (not np.isnan(hp) and hp <= EMO["kill_hp_ret"]) or (not np.isnan(kr) and kr >= 0.85)
        nxt, why = cur, ""
        if stay >= EMO["min_stay"]:
            if cur == "低位试错":
                if hot and d3 > 0 and H >= EMO["main_min_height"]:
                    nxt, why = "主升", f"水温升到{x:.0f}且空间打到{H}板，合力形成"
                elif cold and killed:
                    nxt, why = "主跌", f"试错失败：水温{x:.0f}，高位继续被杀"
            elif cur == "主升":
                if cold:
                    nxt, why = "主跌", f"水温骤降到{x:.0f}"
                elif killed:
                    nxt, why = "高位震荡", "高位股出现亏钱效应，进入分歧"
                elif d3 <= -12 and not hot:
                    nxt, why = "高位震荡", f"水温从高位回落到{x:.0f}，出现分歧"
            elif cur == "高位震荡":
                if cold or (killed and x < 45):
                    nxt, why = "主跌", f"分歧转杀跌：水温{x:.0f}"
                elif hot and d3 > 0 and H > peak:
                    nxt, why = "主升", f"穿越：空间打到新高度{H}板"
            elif cur == "主跌":
                dr = dt_rank[t]
                if not cold and d3 > 0 and not killed and (np.isnan(dr) or dr < 0.6):
                    nxt, why = "低位试错", f"跌停减少、水温回升到{x:.0f}，进入试错期"
        if nxt != cur:
            if nxt in ("低位试错", "主跌"):
                peak = 0
            cur, stay = nxt, 0
        else:
            stay += 1
        if cur == "主升":
            peak = max(peak, H)
        phase[t], notes[t] = cur, why
    return phase, notes


# ---------------------------------------------------------------- 查询
DAILY_COLS = ["date", "phase", "temp", "temp_raw", "cap", "zt", "dt", "zb", "zb_rate", "height", "lb", "promo1",
              "promo_lb", "prev_zt_ret", "prev_lb_ret", "damian", "hp_ret", "mid_ret", "kill", "up_ratio", "amount",
              "note"]


def phase_map():
    with db.session() as c:
        rows = c.execute("SELECT date, phase, cap FROM emo_daily WHERE phase IS NOT NULL").fetchall()
    return {d: (p, cap) for d, p, cap in rows}


def series(start=None, end=None):
    sql = f"SELECT {','.join(DAILY_COLS)} FROM emo_daily WHERE phase IS NOT NULL"
    args = []
    if start:
        sql += " AND date>=?"
        args.append(start)
    if end:
        sql += " AND date<=?"
        args.append(end)
    with db.session() as c:
        rows = c.execute(sql + " ORDER BY date", args).fetchall()
    return [dict(zip(DAILY_COLS, r)) for r in rows]


def day_detail(date=None):
    with db.session() as c:
        if not date:
            r = c.execute("SELECT MAX(date) FROM emo_daily WHERE phase IS NOT NULL").fetchone()
            date = r[0]
        else:
            r = c.execute("SELECT MAX(date) FROM emo_daily WHERE date<=? AND phase IS NOT NULL", (date,)).fetchone()
            date = r[0]
        if not date:
            return None
        row = c.execute(f"SELECT {','.join(DAILY_COLS)}, ladder, mainline, old_themes FROM emo_daily WHERE date=?",
                        (date,)).fetchone()
        names = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
        trans = c.execute("SELECT date, phase, note FROM emo_daily WHERE date<=? AND note<>'' ORDER BY date DESC "
                          "LIMIT 1", (date,)).fetchone()
        th = c.execute("SELECT theme, zt, zt5, heat, rank5, leader, leader_board, is_old, is_new FROM emo_theme "
                       "WHERE date=? ORDER BY zt5 DESC, zt DESC LIMIT 25", (date,)).fetchall()
        pk = c.execute("SELECT mode, code, theme, board, score, fit, detail, buyable, oret1, oret3, oret5, oret10 "
                       "FROM emo_picks WHERE date=? ORDER BY mode, score DESC", (date,)).fetchall()
        meta = db.get_meta("themes_update") or {}
    d = dict(zip(DAILY_COLS, row[:len(DAILY_COLS)]))
    ladder = [{"board": b, "code": cd, "name": names.get(cd, ""), "theme": t} for b, cd, t in json.loads(row[-3])]
    picks = {"龙头": [], "补涨": [], "切换": []}
    for mode, code, theme, b, sc, fit, det, buyable, o1, o3, o5, o10 in pk:
        det = json.loads(det)
        if "题材龙头" in det:
            det["题材龙头"] = f"{names.get(det['题材龙头'], '')} {det['题材龙头']}"
        picks.setdefault(mode, []).append({"code": code, "name": names.get(code, ""), "theme": theme, "board": b,
                                           "score": sc, "fit": bool(fit), "detail": det,
                                           "buyable": None if buyable is None else bool(buyable),
                                           "oret1": o1, "oret3": o3, "oret5": o5, "oret10": o10})
    ph = d["phase"]
    return {
        "date": date, "metrics": d, "advice": ADVICE.get(ph, ""), "recommend": RECOMMEND.get(ph, []),
        "cap": PHASE_CAP.get(ph), "transition": {"date": trans[0], "phase": trans[1], "note": trans[2]} if trans else None,
        "ladder": ladder, "mainline": json.loads(row[-2]), "old_themes": json.loads(row[-1]),
        "themes": [{"theme": a, "zt": b, "zt5": z5, "heat": h, "rank5": rk, "leader": ld,
                    "leader_name": names.get(ld, "") if ld else "", "leader_board": lb, "old": bool(o), "new": bool(n)}
                   for a, b, z5, h, rk, ld, lb, o, n in th],
        "picks": picks, "theme_source": meta.get("source"), "theme_note": meta.get("note"),
        "phases": list(PHASES), "parts": TEMP_PARTS,
    }
