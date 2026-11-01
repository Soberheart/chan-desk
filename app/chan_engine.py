"""对 chan.py 的封装：逐根喂K线（与实盘一致，无未来函数）、序列化缠论元素、扫描时点信号。"""
import json
import math

import numpy as np
import pandas as pd

from . import settings  # noqa: F401  (把 chan 目录加入 sys.path)
from .scoring import score_buy
from Chan import CChan  # noqa: E402
from ChanConfig import CChanConfig  # noqa: E402
from Common.CEnum import DATA_FIELD, FX_TYPE, KL_TYPE  # noqa: E402
from Common.CTime import CTime  # noqa: E402
from KLine.KLine_Unit import CKLine_Unit  # noqa: E402

LEVELS = {"day": KL_TYPE.K_DAY, "30m": KL_TYPE.K_30M}


def _num(x):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x


def make_klu(t: str, o, h, l, c, v=0.0):
    if len(t) > 10:
        ct = CTime(int(t[:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]))
    else:
        ct = CTime(int(t[:4]), int(t[5:7]), int(t[8:10]), 0, 0)
    h = max(h, o, c, l)
    l = min(l, o, c, h)
    return CKLine_Unit({DATA_FIELD.FIELD_TIME: ct, DATA_FIELD.FIELD_OPEN: float(o), DATA_FIELD.FIELD_HIGH: float(h),
                        DATA_FIELD.FIELD_LOW: float(l), DATA_FIELD.FIELD_CLOSE: float(c),
                        DATA_FIELD.FIELD_VOLUME: float(v or 0)})


def new_chan(levels=("day",), conf=None):
    cfg = dict(settings.CHAN_CONFIG)
    if conf:
        cfg.update(conf)
    cfg["trigger_step"] = True
    return CChan(code="local", data_src="custom:none.none", lv_list=[LEVELS[x] for x in levels],
                 config=CChanConfig(cfg))


def tstr(ct: CTime):
    if ct.hour == 0 and ct.minute == 0:
        return f"{ct.year:04}-{ct.month:02}-{ct.day:02}"
    return f"{ct.year:04}-{ct.month:02}-{ct.day:02} {ct.hour:02}:{ct.minute:02}"


class Stepper:
    """逐根推进的缠论计算器，支持日线 + 30 分钟联立（区间套）。"""

    def __init__(self, day_df: pd.DataFrame, m30_df: pd.DataFrame | None = None, conf=None):
        self.day = day_df.reset_index(drop=True)
        self.multi = m30_df is not None and not m30_df.empty
        self.levels = ("day", "30m") if self.multi else ("day",)
        self.m30_groups = {}
        if self.multi:
            m = m30_df.copy()
            m["d"] = m["time"].str[:10]
            self.m30_groups = {d: g for d, g in m.groupby("d")}
        self.conf = conf
        self.reset()

    def reset(self):
        self.chan = new_chan(self.levels, self.conf)
        self.pos = 0  # 已喂入的日K数量

    def feed_one(self):
        r = self.day.iloc[self.pos]
        inp = {KL_TYPE.K_DAY: [make_klu(r.date, r.open, r.high, r.low, r.close, r.volume)]}
        if self.multi:
            g = self.m30_groups.get(r.date)
            subs = [] if g is None else [make_klu(x.time, x.open, x.high, x.low, x.close, x.volume)
                                          for x in g.itertuples()]
            inp[KL_TYPE.K_30M] = subs
        self.chan.trigger_load(inp)
        self.pos += 1

    def goto(self, n):
        n = max(1, min(n, len(self.day)))
        if n < self.pos:
            self.reset()
        while self.pos < n:
            self.feed_one()


# ---------------------------------------------------------------- 序列化
def serialize_level(chan, lv_idx, window=None):
    kl = chan[lv_idx]
    klus = list(kl.klu_iter())
    if not klus:
        return {"bars": []}
    start = 0 if window is None else max(0, len(klus) - window)
    first_idx = klus[start].idx
    bars = []
    for k in klus[start:]:
        m = getattr(k, "macd", None)
        bars.append([tstr(k.time), k.open, k.close, k.low, k.high,
                     _num(round(m.DIF, 4)) if m else None, _num(round(m.DEA, 4)) if m else None,
                     _num(round(m.macd, 4)) if m else None])

    def rel(klu):
        return klu.idx - first_idx

    def line(obj):
        b, e = obj.get_begin_klu(), obj.get_end_klu()
        return {"x1": rel(b), "y1": obj.get_begin_val(), "x2": rel(e), "y2": obj.get_end_val(),
                "sure": bool(obj.is_sure)}

    bis = [line(b) for b in kl.bi_list if b.get_end_klu().idx >= first_idx]
    segs = [line(s) for s in kl.seg_list if s.get_end_klu().idx >= first_idx]
    zss = [{"x1": rel(z.begin), "x2": rel(z.end), "low": z.low, "high": z.high, "sure": bool(z.is_sure)}
           for z in kl.zs_list if z.end.idx >= first_idx]
    bsps = []
    for b in kl.bs_point_lst.get_latest_bsp(0):
        if b.klu.idx < first_idx:
            continue
        bsps.append({"x": rel(b.klu), "price": b.klu.low if b.is_buy else b.klu.high, "buy": b.is_buy,
                     "types": b.type2str(), "sure": bool(b.bi.is_sure), "time": tstr(b.klu.time)})
    seg_bsps = []
    for b in kl.seg_bs_point_lst.get_latest_bsp(0):
        if b.klu.idx < first_idx:
            continue
        seg_bsps.append({"x": rel(b.klu), "price": b.klu.low if b.is_buy else b.klu.high, "buy": b.is_buy,
                         "types": b.type2str(), "sure": bool(b.bi.is_sure), "time": tstr(b.klu.time)})
    return {"bars": bars, "bi": bis, "seg": segs, "zs": zss, "bsp": bsps, "seg_bsp": seg_bsps}


def serialize(chan, levels, windows=(260, 400)):
    return {lv: serialize_level(chan, i, windows[i] if i < len(windows) else None) for i, lv in enumerate(levels)}


# ---------------------------------------------------------------- 时点信号扫描
def scan_signals(code, df: pd.DataFrame, signal_start: str):
    """逐根回放全历史，记录每个买卖点“第一次被发现”的日期及当时的打分，返回信号行列表。"""
    if len(df) < 30:
        return []
    df = df.reset_index(drop=True)
    close = df["close"].to_numpy(float)
    opn = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    ma60 = pd.Series(close).rolling(60, min_periods=20).mean().to_numpy()
    vol20 = pd.Series(vol).rolling(20, min_periods=5).mean().to_numpy()
    dates = df["date"].tolist()
    n = len(df)

    chan = new_chan(("day",))
    kl = None
    seen = {}
    rows = []
    for i, r in enumerate(df.itertuples(index=False)):
        chan.trigger_load({KL_TYPE.K_DAY: [make_klu(r.date, r.open, r.high, r.low, r.close, r.volume)]})
        if kl is None:
            kl = chan[0]
        if r.date < signal_start:
            for b in kl.bs_point_lst.get_latest_bsp(4):
                seen[(b.klu.idx, b.is_buy)] = set(t.value for t in b.type)
            continue
        last_klc_idx = kl[-1].idx
        for b in kl.bs_point_lst.get_latest_bsp(4):
            # 只在买卖点所在K线的分型已经确认（后面至少还有一根合并K线）时才记为信号，
            # 避免向下延伸中的笔每创新低就“出现”一个新一买
            klc = b.klu.klc
            if klc.idx >= last_klc_idx or klc.fx != (FX_TYPE.BOTTOM if b.is_buy else FX_TYPE.TOP):
                continue
            key = (b.klu.idx, b.is_buy)
            types = set(t.value for t in b.type)
            old = seen.get(key)
            if old is not None and types <= old:
                continue
            seen[key] = types | (old or set())
            bi_ = b.klu.idx
            price = b.klu.low if b.is_buy else b.klu.high
            feats = {k: _num(v) for k, v in b.features.items()}
            tlist = sorted(types, key=lambda t: ["1", "1p", "2", "2s", "3a", "3b"].index(t))
            score, detail = None, {}
            if b.is_buy:
                zs_margin = None
                if any(t in ("3a", "3b") for t in types):
                    zs_before = [z for z in kl.zs_list if z.end.idx < bi_]
                    if zs_before:
                        z = zs_before[-1]
                        zs_margin = (price - z.high) / z.high
                ctx = {
                    "is_sure": b.bi.is_sure,
                    "lag": i - bi_,
                    "has_bsp1": b.relate_bsp1 is not None,
                    "gain_since_bsp": close[i] / price - 1 if price > 0 else 0,
                    "vol_ratio_bsp": vol[bi_] / vol20[bi_] if bi_ < n and vol20[bi_] and vol20[bi_] > 0 else None,
                    "vol_ratio_now": vol[i] / vol20[i] if vol20[i] and vol20[i] > 0 else None,
                    "above_ma60": bool(close[i] > ma60[i]) if not np.isnan(ma60[i]) else None,
                    "zs_margin": zs_margin,
                }
                score, detail = score_buy(tlist, feats, ctx)
                detail = {"分项": detail, "特征": {k: round(v, 4) for k, v in feats.items() if v is not None},
                          "距买点涨幅": round(ctx["gain_since_bsp"], 4)}

            def fwd(h):
                return close[i + h] / close[i] - 1 if i + h < n else None

            def ofwd(h):
                return close[i + h] / opn[i + 1] - 1 if i + h < n else None

            seg = slice(i + 1, min(n, i + 11))
            mdd = low[seg].min() / close[i] - 1 if i + 1 < n else None
            mfe = high[seg].max() / close[i] - 1 if i + 1 < n else None
            rows.append((code, r.date, dates[bi_] if bi_ < n else r.date, int(b.is_buy), ",".join(tlist),
                         int(b.bi.is_sure), price, close[i], price if b.is_buy else None, score,
                         json.dumps(detail, ensure_ascii=False), fwd(5), fwd(10), fwd(20),
                         ofwd(5), ofwd(10), ofwd(20), _num(mdd), _num(mfe)))
    return rows
