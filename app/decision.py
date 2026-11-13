"""决策台：把“节奏”规则变成每天收盘后的检查清单。

1. 今日节奏：这次下跌是分歧还是退潮？有没有弱转强、冰点？按阶段的仓位上限。
2. 持仓检查：每只持仓按卖点规则逐条检查（止损价、放量大阴、5日线、补涨不板、同批掉队、相对板块走弱），
   冰点时提示“别在恐慌尾盘砍，等反抽”。
3. 换强候选：主线题材里在分歧日最抗跌、位置合适的票，按“单吊四条”打勾。
4. 盘前三行计划、交易日志（自动记下当时的阶段和判断，事后算 1/3/5 日结果，统计自己在哪种情况下最容易错）。

所有判断只用收盘后的日线数据，是盘后复盘、盘前计划用的，不是盘中信号。
"""
import datetime as dt
import json

from . import db
from .limits import is_limit_up
from .settings import PHASE_CAP

ROLES = ("龙头", "中军", "趋势", "补涨", "低吸试错")
ACTIONS = ("买入", "加仓", "减仓", "清仓")

ADVICE = {
    "弱转强": "加仓窗口：下跌后出现弱转强，可以按计划加仓或换强，买确定性，不抄最低点。",
    "分歧": "分歧日：不加仓、不追，持有；记下谁最抗跌、最先翻红，作为明天换强的候选。",
    "退潮": "退潮：不加仓、不抄底；破位的按止损走，仓位降到阶段上限以内。",
    "冰点": "冰点：恐慌到了极致。别在尾盘割肉，要减就等明天反抽时减；开始准备低位试错名单。",
    "一致": "一致上涨：持有，不追高位；盯紧卖点信号，尤其是同批对比和相对走弱。",
    "震荡": "没有明显方向：按计划持有，不做计划外的加减仓。",
}


# ---------------------------------------------------------------- 工具
def _latest(c, date=None):
    if date:
        r = c.execute("SELECT MAX(date) FROM emo_daily WHERE date<=? AND phase IS NOT NULL", (date,)).fetchone()
    else:
        r = c.execute("SELECT MAX(date) FROM emo_daily WHERE phase IS NOT NULL").fetchone()
    return r[0]


def _bars(c, code, date, n=30):
    rows = c.execute("SELECT date, open, high, low, close, volume, amount, pct_chg, is_st FROM kline_d "
                     "WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?", (code, date, n)).fetchall()
    rows = rows[::-1]
    out = []
    for i, r in enumerate(rows):
        d, o, h, lo, cl, v, a, p, st = r
        if p is None and i > 0 and rows[i - 1][4]:
            p = cl / rows[i - 1][4] - 1
        out.append({"date": d, "open": o, "high": h, "low": lo, "close": cl, "vol": v or 0, "amount": a or 0,
                    "pct": p, "st": bool(st)})
    return out


def _ma(bars, n, key="close", end=None):
    end = len(bars) if end is None else end
    seg = bars[max(0, end - n):end]
    return sum(b[key] for b in seg) / len(seg) if len(seg) == n else None


def _shadow(b):
    rng = b["high"] - b["low"]
    return (b["high"] - max(b["open"], b["close"])) / rng if rng > 0 else 0.0


def std_code(c, q):
    q = (q or "").strip()
    if not q:
        return None
    from .themes import _std_code
    if any(ch.isdigit() for ch in q):
        code = _std_code(q)
        if code and c.execute("SELECT 1 FROM stocks WHERE code=?", (code,)).fetchone():
            return code
    r = c.execute("SELECT code FROM stocks WHERE name=? OR name LIKE ? ORDER BY length(name) LIMIT 1",
                  (q, f"%{q}%")).fetchone()
    return r[0] if r else None


def stock_theme(c, code, date):
    """该股所属题材里，当天 5 日涨停数最多的那个。"""
    r = c.execute("SELECT t.theme FROM themes t LEFT JOIN emo_theme e ON e.theme=t.theme AND e.date=? "
                  "WHERE t.code=? ORDER BY COALESCE(e.zt5,0) DESC, COALESCE(e.zt,0) DESC LIMIT 1",
                  (date, code)).fetchone()
    return r[0] if r else None


def theme_avg(c, theme, date):
    r = c.execute("SELECT AVG(k.pct_chg), COUNT(*) FROM themes t JOIN kline_d k ON k.code=t.code AND k.date=? "
                  "WHERE t.theme=? AND COALESCE(k.is_st,0)=0 AND k.pct_chg IS NOT NULL", (date, theme)).fetchone()
    return (r[0], r[1]) if r and r[1] else (None, 0)


# ---------------------------------------------------------------- 1. 今日节奏
def market(date=None):
    with db.session() as c:
        date = _latest(c, date)
        if not date:
            return None
        cols = ["date", "phase", "temp", "cap", "zt", "dt", "zb_rate", "height", "prev_zt_ret", "damian",
                "hp_ret", "kill", "up_ratio"]
        rows = [dict(zip(cols, r)) for r in c.execute(
            f"SELECT {','.join(cols)} FROM emo_daily WHERE date<=? AND phase IS NOT NULL ORDER BY date DESC LIMIT 8",
            (date,))]
        prev_d = rows[1]["date"] if len(rows) > 1 else None
        hi = []
        if prev_d:
            yh = c.execute("SELECT MAX(board) FROM ladder WHERE date=? AND kind='zt'", (prev_d,)).fetchone()[0] or 0
            names = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
            hi = [{"code": cd, "name": names.get(cd, ""), "board": b, "zt": z, "pct": p}
                  for cd, b, z, p in c.execute(
                      "SELECT code, board, nxt_zt, nxt_pct FROM ladder WHERE date=? AND kind='zt' AND board>=? "
                      "ORDER BY board DESC", (prev_d, max(3, yh - 1)))]
    t, prev = rows[0], rows[1:]
    sig = []

    def add(name, value, side, why):
        sig.append({"name": name, "value": value, "side": side, "why": why})

    # 高标
    known = [h for h in hi if h["pct"] is not None]
    if known:
        avg = sum(h["pct"] for h in known) / len(known)
        promoted = [h for h in known if h["zt"]]
        val = f"{len(known)} 只昨日高标，今天平均 {avg * 100:+.1f}%，晋级 {len(promoted)} 只"
        if promoted or avg >= -0.02:
            add("龙头和高标", val, "分歧", "高标抗跌或继续晋级，资金还在")
        elif avg <= -0.05 or min(h["pct"] for h in known) <= -0.09:
            add("龙头和高标", val, "退潮", "高标被杀、出现大面")
        else:
            add("龙头和高标", val, "中性", "高标走弱但没有被杀")
    # 跌停
    avg3 = sum(p["dt"] for p in prev[:3]) / max(len(prev[:3]), 1) if prev else t["dt"]
    val = f"今天 {t['dt']} 家，前三天平均 {avg3:.0f} 家"
    if t["dt"] >= max(10, 1.5 * avg3):
        add("跌停家数", val, "退潮", "跌停在扩散")
    elif t["dt"] <= max(5, avg3):
        add("跌停家数", val, "分歧", "跌停可控，没有扩散")
    else:
        add("跌停家数", val, "中性", "跌停略有增加")
    # 昨日涨停今日
    pz = t["prev_zt_ret"]
    if pz is not None:
        val = f"平均 {pz * 100:+.2f}%，大面 {t['damian']} 家"
        if pz <= -0.01 or t["damian"] >= 10:
            add("昨日涨停今日表现", val, "退潮", "打板亏钱，接力资金受伤")
        elif pz >= 0:
            add("昨日涨停今日表现", val, "分歧", "打板还有溢价")
        else:
            add("昨日涨停今日表现", val, "中性", "小幅亏钱")
    # 连板高度
    mp = max((p["height"] for p in prev[:3]), default=t["height"])
    val = f"今天 {t['height']} 板，前三天最高 {mp} 板"
    if t["height"] <= 3 and mp >= 5:
        add("连板高度", val, "退潮", "高度被压缩")
    elif t["height"] >= mp - 1:
        add("连板高度", val, "分歧", "高度保持")
    else:
        add("连板高度", val, "中性", "高度下降")

    n_ret = sum(s["side"] == "退潮" for s in sig)
    up = t["up_ratio"] if t["up_ratio"] is not None else 0.5
    down_day = up < 0.4
    downs = 0
    for r in rows:
        if r["up_ratio"] is not None and r["up_ratio"] < 0.45:
            downs += 1
        else:
            break
    y = prev[0] if prev else None
    weak_to_strong = bool(
        y and (y["up_ratio"] or 0.5) < 0.45 and up >= 0.5 and (pz or 0) >= 0.005
        and t["dt"] <= max(5, 0.6 * (y["dt"] or 0)) and (not known or any(h["zt"] for h in known)
                                                          or sum(h["pct"] for h in known) / len(known) >= 0))
    ice = (t["temp"] or 50) <= 25 and (downs >= 3 or t["dt"] >= 2 * max(avg3, 5))
    if ice:
        verdict = "冰点"
    elif weak_to_strong:
        verdict = "弱转强"
    elif n_ret >= 3 or (n_ret >= 2 and t["phase"] in ("主跌", "高位震荡")):
        verdict = "退潮"
    elif down_day:
        verdict = "分歧"
    elif up >= 0.6:
        verdict = "一致"
    else:
        verdict = "震荡"
    cap = PHASE_CAP.get(t["phase"], 1.0)
    return {"date": date, "phase": t["phase"], "temp": t["temp"], "cap": cap, "verdict": verdict,
            "advice": ADVICE[verdict], "signals": sig, "down_days": downs, "up_ratio": up,
            "high_boards": hi[:12], "retreat_votes": n_ret}


# ---------------------------------------------------------------- 1b. 指数过滤
def index_state(date=None):
    """站上 20 日线且 20 日线向上 → 可以开仓；两者都不满足 → 不宜开仓；其余 → 轻仓。"""
    from .settings import MARKET_INDEXES
    out = []
    with db.session() as c:
        date = _latest(c, date)
        for code, name in MARKET_INDEXES.items():
            cl = [r[0] for r in c.execute("SELECT close FROM index_d WHERE code=? AND date<=? ORDER BY date DESC "
                                          "LIMIT 30", (code, date))][::-1]
            if len(cl) < 26:
                continue
            ma5, ma20 = sum(cl[-5:]) / 5, sum(cl[-20:]) / 20
            ma20_5 = sum(cl[-25:-5]) / 20
            pct = cl[-1] / cl[-2] - 1
            downs = 0
            for i in range(len(cl) - 1, 0, -1):
                if cl[i] < cl[i - 1]:
                    downs += 1
                else:
                    break
            above, rising = cl[-1] >= ma20, ma20 >= ma20_5
            if above and rising:
                v = "可以开仓"
            elif not above and not rising:
                v = "不宜开仓"
            else:
                v = "轻仓"
            why = (f"收盘{'站上' if above else '跌破'}20日线，20日线{'向上' if rising else '向下'}；"
                   f"今天 {pct * 100:+.2f}%，{'连跌 %d 天' % downs if downs >= 2 else ''}")
            if pct <= -0.015 and v != "不宜开仓":
                v, why = "轻仓", why + "；今天大跌，不追"
            out.append({"code": code, "name": name, "close": cl[-1], "pct": pct, "ma5": ma5, "ma20": ma20,
                        "verdict": v, "why": why.rstrip("，；")})
    main = out[0]["verdict"] if out else None
    return {"rows": out, "verdict": main}


# ---------------------------------------------------------------- 1c. 题材周期：龙 → 补 → 切 → 空
STAGE_ADVICE = {
    "龙头期": ("做龙头和核心：分歧日抗跌、弱转强时买；不去做低位跟风",
            "龙头断板收阴、同批掉队、连续两天弱于板块 → 走"),
    "龙头期·分歧": ("今天不加不追，记下最抗跌、最先翻红或回封的票，等弱转强",
                "持仓明显弱于板块、或跌破5日线的先减"),
    "龙头期·弱转强": ("买点：分歧后转强，买分歧日最抗跌的龙头或中军，止损放在分歧日低点下",
                 "按原卖点持有"),
    "补涨期": ("资金切到低位补涨，是鱼尾行情：不再开新仓；补涨只做次日不板就走",
            "龙头和中军逢高减仓，准备撤退"),
    "滞涨退潮": ("不抄底、不加仓，老题材的反弹大多是诱多",
             "这个题材的持仓清掉，空仓等下一个主升题材"),
    "空仓等待": ("没有行情，不参与；等新的主升题材出现（看下方“新题材”）", "不在这个题材持仓"),
}


def theme_state(c, theme, date, prev_dates):
    hist = c.execute("SELECT date, zt, zt5, leader, leader_board FROM emo_theme WHERE theme=? AND date<=? "
                     "ORDER BY date DESC LIMIT 10", (theme, date)).fetchall()
    today = hist[0] if hist and hist[0][0] == date else (date, 0, 0, None, None)
    zt, zt5 = today[1], today[2]
    peak5 = max([h[2] for h in hist] + [0])
    lead_max = max([h[4] or 0 for h in hist[:5]] + [0])
    ta, n = theme_avg(c, theme, date)
    up = c.execute("SELECT AVG(k.pct_chg>0) FROM themes t JOIN kline_d k ON k.code=t.code AND k.date=? "
                   "WHERE t.theme=? AND COALESCE(k.is_st,0)=0", (date, theme)).fetchone()[0]
    up = up if up is not None else 0.5
    dts = {d: c.execute("SELECT COUNT(*) FROM ladder l JOIN themes t ON t.code=l.code AND t.theme=? "
                        "WHERE l.date=? AND l.kind='dt'", (theme, d)).fetchone()[0] for d in [date] + prev_dates}
    sig = []

    def add(name, value, side):
        sig.append({"name": name, "value": value, "side": side})

    y = prev_dates[0] if prev_dates else None
    broken, yz_ret = False, None
    if y:
        hi = c.execute("SELECT l.board, l.nxt_zt, l.nxt_pct FROM ladder l JOIN themes t ON t.code=l.code AND t.theme=? "
                       "WHERE l.date=? AND l.kind='zt' AND l.nxt_pct IS NOT NULL ORDER BY l.board DESC LIMIT 3",
                       (theme, y)).fetchall()
        if hi:
            avg = sum(h[2] for h in hi) / len(hi)
            pro = sum(1 for h in hi if h[1])
            broken = not pro and (avg <= -0.05 or min(h[2] for h in hi) <= -0.09)
            add("龙头和前排", f"昨日最高 {hi[0][0]} 板，前排今天平均 {avg * 100:+.1f}%，晋级 {pro} 只",
                "退潮" if broken else ("分歧" if pro or avg >= -0.02 else "中性"))
        yz = c.execute("SELECT AVG(l.nxt_pct), COUNT(*) FROM ladder l JOIN themes t ON t.code=l.code AND t.theme=? "
                       "WHERE l.date=? AND l.kind='zt' AND l.nxt_pct IS NOT NULL", (theme, y)).fetchone()
        if yz and yz[1]:
            yz_ret = yz[0]
            add("昨日涨停今日", f"{yz[1]} 只，平均 {yz_ret * 100:+.2f}%",
                "退潮" if yz_ret <= -0.01 else ("分歧" if yz_ret >= 0 else "中性"))
    add("涨停数", f"今天 {zt} 只，5 日 {zt5} 只，近 10 日 5 日峰值 {peak5} 只",
        "退潮" if peak5 >= 5 and zt5 <= 0.4 * peak5 else "分歧")
    dprev = sum(dts.get(d, 0) for d in prev_dates) / max(len(prev_dates), 1)
    add("跌停数", f"今天 {dts[date]} 只，前三天平均 {dprev:.1f} 只",
        "退潮" if dts[date] >= max(2, 2 * dprev) else "分歧")
    # 中军 vs 低位首板 → 是否切到补涨
    big = c.execute("SELECT k.pct_chg FROM themes t JOIN kline_d k ON k.code=t.code AND k.date=? WHERE t.theme=? "
                    "AND COALESCE(k.is_st,0)=0 AND k.pct_chg IS NOT NULL ORDER BY k.amount DESC LIMIT 5",
                    (date, theme)).fetchall()
    big_avg = sum(r[0] for r in big) / len(big) if big else None
    firsts = [r[0] for r in c.execute("SELECT l.code FROM ladder l JOIN themes t ON t.code=l.code AND t.theme=? "
                                      "WHERE l.date=? AND l.kind='zt' AND l.board=1", (theme, date))]
    low1 = 0
    for code in firsts:
        bars = _bars(c, code, date, 22)
        if len(bars) >= 22 and bars[-22]["close"] and bars[-2]["close"] / bars[-22]["close"] - 1 < 0.25:
            low1 += 1
    rotation = bool(big_avg is not None and zt >= 3 and big_avg <= 0.005 and low1 >= max(2, 0.5 * zt))
    if big_avg is not None:
        add("中军 vs 低位首板", f"成交额前五平均 {big_avg * 100:+.1f}%，低位首板 {low1} 只",
            "补涨" if rotation else "分歧")

    if zt5 < 3 and peak5 < 5:
        stage = "空仓等待"
    elif peak5 >= 5 and (zt5 <= 0.4 * peak5 or (broken and ((yz_ret or 0) <= -0.01 or dts[date] >= 2))):
        stage = "滞涨退潮"
    elif rotation and lead_max >= 3:
        stage = "补涨期"
    else:
        zt_y = hist[1][1] if len(hist) > 1 and hist[1][0] == y else 0
        up_y = c.execute("SELECT AVG(k.pct_chg>0) FROM themes t JOIN kline_d k ON k.code=t.code AND k.date=? "
                         "WHERE t.theme=?", (y, theme)).fetchone()[0] if y else None
        # 弱转强：昨天板块分歧（多数下跌），今天多数上涨、涨停增加、昨日涨停没有亏钱
        if up_y is not None and up_y < 0.45 and up >= 0.55 and zt > zt_y and (yz_ret is None or yz_ret >= 0):
            stage = "龙头期·弱转强"
        elif up < 0.45 or (ta or 0) < -0.01:
            stage = "龙头期·分歧"
        else:
            stage = "龙头期"
    buy, sell = STAGE_ADVICE[stage]
    return {"theme": theme, "stage": stage, "avg": ta, "up": up, "zt": zt, "zt5": zt5, "peak5": peak5,
            "lead_max": lead_max, "leader": today[3], "members": n, "signals": sig, "buy": buy, "sell": sell,
            "open_ok": stage in ("龙头期", "龙头期·弱转强")}


def themes_diag(date=None, extra=(), mk=None, ix=None):
    with db.session() as c:
        date = _latest(c, date)
        if not date:
            return {"rows": [], "new": []}
        prev = [r[0] for r in c.execute("SELECT date FROM emo_daily WHERE date<? ORDER BY date DESC LIMIT 3", (date,))]
        names = [r[0] for r in c.execute("SELECT theme FROM emo_theme WHERE date=? ORDER BY zt5 DESC, zt DESC LIMIT 5",
                                         (date,))]
        held = [stock_theme(c, r[0], date) for r in c.execute("SELECT code FROM dk_holdings WHERE theme IS NULL")]
        held += [r[0] for r in c.execute("SELECT theme FROM dk_holdings WHERE theme IS NOT NULL")]
        order = list(dict.fromkeys([t for t in list(extra) + held + names if t]))
        known = {r[0] for r in c.execute("SELECT DISTINCT theme FROM themes")}
        allnames = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
        rows = []
        for t in [t for t in order if t in known][:10]:
            st = theme_state(c, t, date, prev)
            st["leader_name"] = allnames.get(st["leader"], "") if st["leader"] else ""
            # 能否开仓 = 指数允许 + 情绪不在主跌 + 题材在龙头期
            gate = []
            if ix and ix["verdict"] == "不宜开仓":
                gate.append("指数不宜开仓")
            if mk and mk["phase"] == "主跌":
                gate.append("情绪主跌")
            if not st["open_ok"]:
                gate.append(f"题材在{st['stage']}")
            if gate:
                st["open"] = "不开仓"
            elif ix and ix["verdict"] == "轻仓":
                st["open"] = "轻仓"
            else:
                st["open"] = "可以开仓"
            st["gate"] = gate
            st["held"], st["picked"] = t in held, t in extra
            rows.append(st)
        new = [{"theme": a, "zt": b, "leader": allnames.get(cd, "") if cd else ""} for a, b, cd in c.execute(
            "SELECT theme, zt, leader FROM emo_theme WHERE date=? AND is_new=1 ORDER BY zt DESC LIMIT 5", (date,))]
    return {"rows": rows, "new": new}


# ---------------------------------------------------------------- 2. 持仓检查
SEV = {"持有": 0, "提示": 1, "警告": 2, "减半": 3, "清仓": 4}


def check_holding(c, h, date, mk):
    code = h["code"]
    bars = _bars(c, code, date, 30)
    res = {"code": code, "rules": [], "status": "持有"}
    if not bars or bars[-1]["date"] != date:
        res.update(status="提示", rules=[{"sev": "提示", "rule": "数据", "why": "今天没有这只票的行情（停牌或数据未更新）"}])
        return res
    b, by = bars[-1], bars[-2] if len(bars) > 1 else None
    ma5, ma5y = _ma(bars, 5), _ma(bars, 5, end=len(bars) - 1)
    vma5 = _ma(bars, 5, "vol", end=len(bars) - 1)
    res.update(close=b["close"], pct=b["pct"], ma5=ma5,
               pnl=(b["close"] / h["cost"] - 1) if h.get("cost") else None)
    rules = []

    def add(sev, rule, why, hard=False):
        rules.append({"sev": sev, "rule": rule, "why": why, "hard": hard})

    if h.get("stop") and b["close"] <= h["stop"]:
        add("清仓", "止损价", f"收盘 {b['close']:.2f} 跌破止损价 {h['stop']:.2f}", hard=True)
    if b["pct"] is not None and b["pct"] <= -0.05 and vma5 and b["vol"] >= 1.5 * vma5 and b["close"] < b["open"]:
        add("清仓", "放量大阴", f"跌 {b['pct'] * 100:.1f}%，量是5日均量的 {b['vol'] / vma5:.1f} 倍", hard=True)
    if ma5:
        if b["close"] < ma5 and by and ma5y and by["close"] < ma5y:
            add("清仓", "5日线", f"连续两天收在5日线下（今天 {b['close']:.2f} < {ma5:.2f}），拉不回")
        elif b["close"] < ma5:
            add("警告", "5日线", f"今天首次收在5日线下（{b['close']:.2f} < {ma5:.2f}），明天收不回就走")
    held_today = h.get("buy_date") and h["buy_date"] >= date
    if h.get("role") == "补涨" and not held_today and not is_limit_up(code, date, b["st"], b["pct"], b["close"], b["high"]):
        add("清仓", "补涨不板就走", "补涨是鱼尾行情，今天没涨停")
    # 同批对比
    peers = [p for p in (h.get("peers") or "").replace("，", ",").split(",") if p.strip()]
    if peers:
        prior_hi = max(x["close"] for x in bars[-21:-1]) if len(bars) > 2 else None
        my_new_high = prior_hi is not None and b["close"] >= prior_hi
        shadows = sum(_shadow(x) >= 0.4 for x in bars[-3:])
        strong, pct_list = [], []
        for p in peers:
            pc = std_code(c, p)
            if not pc:
                continue
            pb = _bars(c, pc, date, 25)
            if not pb or pb[-1]["date"] != date:
                continue
            php = max(x["close"] for x in pb[-21:-1]) if len(pb) > 2 else None
            nm = c.execute("SELECT name FROM stocks WHERE code=?", (pc,)).fetchone()
            if pb[-1]["pct"] is not None:
                pct_list.append(pb[-1]["pct"])
            if php and pb[-1]["close"] >= php:
                strong.append(nm[0] if nm else pc)
        if strong and not my_new_high and shadows >= 2:
            add("清仓", "同批掉队", f"同批的 {'、'.join(strong)} 创新高，它卡在前高下、近三天 {shadows} 根长上影")
        elif strong and not my_new_high:
            add("警告", "同批掉队", f"同批的 {'、'.join(strong)} 创新高，它没跟上")
        if pct_list and b["pct"] is not None:
            pavg = sum(pct_list) / len(pct_list)
            if pavg >= 0.02 and b["pct"] - pavg <= -0.03:
                add("减半", "弱于同批", f"同批平均 {pavg * 100:+.1f}%，它 {b['pct'] * 100:+.1f}%")
    # 相对题材
    theme = h.get("theme") or stock_theme(c, code, date)
    res["theme"] = theme
    if theme and b["pct"] is not None:
        ta, n = theme_avg(c, theme, date)
        if ta is not None and n >= 5:
            res["theme_avg"] = ta
            weak_today = ta >= 0.02 and b["pct"] - ta <= -0.04
            weak_y = False
            if by and by["pct"] is not None:
                tay, _ = theme_avg(c, theme, by["date"])
                weak_y = tay is not None and tay >= 0.02 and by["pct"] - tay <= -0.04
            if weak_today and weak_y:
                add("清仓", "相对板块走弱", f"连续两天板块大涨它不涨（今天板块 {ta * 100:+.1f}%，它 {b['pct'] * 100:+.1f}%）")
            elif weak_today:
                add("减半", "相对板块走弱", f"板块「{theme}」平均 {ta * 100:+.1f}%，它 {b['pct'] * 100:+.1f}%")
    # 冰点修正
    if mk and mk["verdict"] == "冰点":
        for r in rules:
            if r["sev"] in ("清仓", "减半") and not r["hard"]:
                r["why"] += "（今天是冰点：别在恐慌尾盘砍，明天反抽时执行）"
    res["rules"] = rules
    if rules:
        res["status"] = max((r["sev"] for r in rules), key=lambda s: SEV[s])
    return res


def holdings(date=None, mk=None):
    with db.session() as c:
        date = _latest(c, date)
        hs = [dict(zip(["code", "cost", "buy_date", "position", "role", "stop", "peers", "theme", "note"], r))
              for r in c.execute("SELECT code, cost, buy_date, position, role, stop, peers, theme, note "
                                 "FROM dk_holdings ORDER BY position DESC")]
        names = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
        out = []
        for h in hs:
            r = check_holding(c, h, date, mk) if date else {"code": h["code"], "rules": [], "status": "提示"}
            out.append({**h, "name": names.get(h["code"], ""), **r})
    total = sum((h["position"] or 0) for h in out)
    port = []
    if mk:
        cap = mk["cap"] * 100
        if total > cap + 1e-6:
            weak = sorted(out, key=lambda h: (h.get("pnl") or 0))
            port.append(f"总仓位 {total:.0f}% 超过「{mk['phase']}」阶段上限 {cap:.0f}%：先减弱的"
                        f"（{'、'.join(x['name'] for x in weak[:2])}）")
    return {"date": date, "rows": out, "total": total, "portfolio": port}


# ---------------------------------------------------------------- 3. 换强候选
def candidates(date=None, mk=None, limit=12):
    with db.session() as c:
        date = _latest(c, date)
        if not date:
            return []
        held = {r[0] for r in c.execute("SELECT code FROM dk_holdings")}
        ths = c.execute("SELECT theme, leader FROM emo_theme WHERE date=? ORDER BY zt5 DESC, zt DESC LIMIT 3",
                        (date,)).fetchall()
        names = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
        boards = {a: b for a, b in c.execute("SELECT code, board FROM ladder WHERE date=? AND kind='zt'", (date,))}
        down_day = bool(mk and mk["up_ratio"] < 0.45)
        out, seen = [], set()
        for theme, leader in ths:
            ta, _ = theme_avg(c, theme, date)
            mem = c.execute("SELECT k.code, k.amount FROM themes t JOIN kline_d k ON k.code=t.code AND k.date=? "
                            "WHERE t.theme=? AND COALESCE(k.is_st,0)=0 AND k.amount>=1e9 ORDER BY k.amount DESC",
                            (date, theme)).fetchall()
            big3 = {m[0] for m in mem[:3]}
            for code, amt in mem:
                if code in seen or code in held:
                    continue
                seen.add(code)
                bars = _bars(c, code, date, 25)
                if len(bars) < 21 or bars[-1]["date"] != date or bars[-1]["pct"] is None:
                    continue
                b = bars[-1]
                ma5, ma10 = _ma(bars, 5), _ma(bars, 10)
                g20 = b["close"] / bars[-21]["close"] - 1
                prior_hi = max(x["close"] for x in bars[-21:-1])
                bd = boards.get(code, 0)
                if code == leader:
                    role = "龙头"
                elif code in big3:
                    role = "中军"
                elif bd in (1, 2) and g20 < 0.25:
                    role = "补涨"
                else:
                    role = "跟风"
                rel = b["pct"] - (ta or 0)
                dist = b["close"] / ma5 - 1 if ma5 else None
                shadow = _shadow(b)
                if b["close"] >= prior_hi:
                    point = "突破前高"
                elif ma5 and b["low"] <= ma5 * 1.01 and b["close"] >= ma5 and g20 >= 0.2:
                    point = "回踩5日线企稳"
                elif ma10 and b["low"] <= ma10 * 1.01 and b["close"] >= ma10 and g20 >= 0.2:
                    point = "回踩10日线企稳"
                else:
                    point = "无明确买点"
                warn = []
                if dist is not None and dist > 0.06:
                    warn.append(f"离5日线 {dist * 100:.0f}%，追高")
                if shadow >= 0.4:
                    warn.append("长上影")
                four = {
                    "主线": True,
                    "主升期": bool(mk and mk["phase"] == "主升"),
                    "龙头/中军": role in ("龙头", "中军"),
                    "分歧日抗跌": bool(down_day and (rel >= 0.02 or b["pct"] >= 0)),
                }
                n4 = sum(four.values())
                size = "可单吊" if n4 == 4 else ("半仓" if n4 == 3 else "观察")
                if role == "补涨" and size != "观察":
                    size = "半仓以下（补涨不单吊）"
                out.append({"code": code, "name": names.get(code, ""), "theme": theme, "role": role, "board": bd,
                            "pct": b["pct"], "rel": rel, "dist": dist, "amount": amt, "point": point, "warn": warn,
                            "four": four, "n4": n4, "size": size})
        out.sort(key=lambda x: (-x["n4"], x["point"] == "无明确买点", bool(x["warn"]), -x["rel"]))
    return out[:limit]


# ---------------------------------------------------------------- 4. 计划与日志
def plan_get(date):
    with db.session() as c:
        r = c.execute("SELECT date, phase, cap, add_signal, cut_signal, note, updated FROM dk_plan WHERE date=?",
                      (date,)).fetchone()
    return dict(zip(["date", "phase", "cap", "add_signal", "cut_signal", "note", "updated"], r)) if r else None


def plan_suggest(mk):
    if not mk:
        return {}
    cap = f"{mk['cap'] * 100:.0f}%"
    adds = {"弱转强": "按计划加仓/换强：优先分歧日最抗跌的主线龙头或中军",
            "分歧": "今天不加；明天如果高标翻红、昨日涨停有溢价、跌停减半 → 弱转强，再加",
            "退潮": "不加仓；等跌停收敛、出现弱转强再说",
            "冰点": "只做低位试错：新题材首板/二板，小仓",
            "一致": "不追高；只在回踩5日线企稳或放量突破时加",
            "震荡": "按计划，不做计划外加仓"}
    return {"phase": mk["phase"], "cap": cap, "add_signal": adds.get(mk["verdict"], ""),
            "cut_signal": "持仓检查里任何一条“清仓”触发就执行：破止损价、放量大阴、5日线两天收不回、同批掉队、连续两天弱于板块"}


def plan_save(d):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with db.session() as c:
        c.execute("INSERT OR REPLACE INTO dk_plan VALUES(?,?,?,?,?,?,?)",
                  (d["date"], d.get("phase"), d.get("cap"), d.get("add_signal"), d.get("cut_signal"),
                   d.get("note"), now))


def holding_save(d):
    with db.session() as c:
        code = std_code(c, d.get("code"))
        if not code:
            raise ValueError("找不到这只股票（输入代码或名称）")
        f = lambda k: float(d[k]) if d.get(k) not in (None, "") else None  # noqa
        c.execute("INSERT OR REPLACE INTO dk_holdings VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (code, f("cost"), d.get("buy_date") or None, f("position"), d.get("role") or None, f("stop"),
                   d.get("peers") or None, d.get("theme") or None, d.get("note") or None,
                   dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
    return code


def holding_delete(code):
    with db.session() as c:
        c.execute("DELETE FROM dk_holdings WHERE code=?", (code,))


def journal_add(d):
    mk = market(d.get("date"))
    with db.session() as c:
        code = std_code(c, d.get("code"))
        if not code:
            raise ValueError("找不到这只股票（输入代码或名称）")
        if d.get("action") not in ACTIONS:
            raise ValueError("操作只能是：" + "、".join(ACTIONS))
        c.execute("INSERT INTO dk_journal(date, code, action, price, position, reason, phase, temp, verdict, created) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (d["date"], code, d["action"], float(d["price"]), float(d.get("position") or 0) or None,
                   d.get("reason") or None, mk["phase"] if mk else None, mk["temp"] if mk else None,
                   mk["verdict"] if mk else None, dt.datetime.now().strftime("%Y-%m-%d %H:%M")))


def journal_delete(jid):
    with db.session() as c:
        c.execute("DELETE FROM dk_journal WHERE id=?", (int(jid),))


def journal():
    with db.session() as c:
        rows = c.execute("SELECT id, date, code, action, price, position, reason, phase, temp, verdict FROM dk_journal "
                         "ORDER BY date DESC, id DESC").fetchall()
        names = {a: b for a, b in c.execute("SELECT code, name FROM stocks")}
        out = []
        for jid, d, code, act, price, pos, reason, ph, temp, vd in rows:
            fut = [r[0] for r in c.execute("SELECT close FROM kline_d WHERE code=? AND date>? ORDER BY date LIMIT 5",
                                           (code, d))]
            rets = {h: (fut[h - 1] / price - 1) if len(fut) >= h and price else None for h in (1, 3, 5)}
            r5 = rets[5]
            right = None
            if r5 is not None:
                right = r5 > 0 if act in ("买入", "加仓") else r5 < 0
            out.append({"id": jid, "date": d, "code": code, "name": names.get(code, ""), "action": act, "price": price,
                        "position": pos, "reason": reason, "phase": ph, "temp": temp, "verdict": vd,
                        "r1": rets[1], "r3": rets[3], "r5": r5, "right": right})
    stats = {}
    for r in out:
        if r["right"] is None:
            continue
        side = "买/加" if r["action"] in ("买入", "加仓") else "减/清"
        k = (side, r["verdict"] or "-")
        s = stats.setdefault(k, {"side": side, "verdict": k[1], "n": 0, "right": 0, "r5": 0.0})
        s["n"] += 1
        s["right"] += int(r["right"])
        s["r5"] += r["r5"]
    st = [{**v, "rate": v["right"] / v["n"], "r5": v["r5"] / v["n"]} for v in stats.values()]
    st.sort(key=lambda x: (x["side"], -x["n"]))
    lessons = []
    for s in st:
        if s["n"] >= 3 and s["rate"] < 0.4:
            if s["side"] == "买/加":
                lessons.append(f"在「{s['verdict']}」日买入/加仓 {s['n']} 次，5 天后只有 {s['rate'] * 100:.0f}% 是赚的")
            else:
                lessons.append(f"在「{s['verdict']}」日减仓/清仓 {s['n']} 次，有 {100 - s['rate'] * 100:.0f}% 卖完 5 天内反而涨了")
    return {"rows": out, "stats": st, "lessons": lessons}


def overview(date=None, themes=()):
    mk = market(date)
    d = mk["date"] if mk else date
    ix = index_state(d)
    return {"market": mk, "index": ix, "themes": themes_diag(d, themes, mk, ix), "holdings": holdings(d, mk),
            "candidates": candidates(d, mk),
            "plan": plan_get(d) if d else None, "suggest": plan_suggest(mk), "roles": ROLES, "actions": ACTIONS}
