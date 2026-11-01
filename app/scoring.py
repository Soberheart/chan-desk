"""缠论买点规则打分（0-100），每一项都给出明细，方便在页面上解释。

  类型分   ≤35  买点类型，多种买点重合（如二三买重合）额外加分
  力度分   ≤25  一买看背驰比例（越小越强），二买看回撤比例，三买看离开中枢的距离
  结构分   ≤15  一买是否为趋势背驰（≥2个中枢），二/三买是否有前置一买
  及时分   ≤10  分型确认得越快（买点到被发现之间的K线越少）分越高
  位置分   ≤15  发现时距买点价格的涨幅（防追高）、量能、与 60 日均线的关系
"""
from .settings import TYPE_BASE


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_buy(types, feats, ctx):
    """types: ['1','2'...]; feats: chan.py 特征字典; ctx: 发现时的行情上下文。"""
    d = {}
    bases = sorted((TYPE_BASE.get(t, 15) for t in types), reverse=True)
    d["类型"] = min(35, bases[0] + 5 * (len(bases) - 1))

    strength = 0.0
    if any(t in ("1", "1p") for t in types):
        r = feats.get("divergence_rate")
        if r is not None and r == r:
            strength = max(strength, 25 * _clip(1 - r))
    if any(t in ("2", "2s") for t in types):
        x = feats.get("bsp2_retrace_rate", feats.get("bsp2s_retrace_rate"))
        if x is not None:
            strength = max(strength, 25 * _clip(1 - x))
    if any(t in ("3a", "3b") for t in types):
        m = ctx.get("zs_margin")
        if m is not None:
            strength = max(strength, 25 * _clip(m / 0.06))
    d["力度"] = round(strength, 1)

    structure = 5
    if any(t in ("1",) for t in types):
        structure = 15 if feats.get("zs_cnt", 1) >= 2 else 9
    elif "1p" in types:
        structure = 7
    elif ctx.get("has_bsp1"):
        structure = 12
    d["结构"] = structure

    lag = ctx.get("lag", 0)
    d["及时"] = 10 if lag <= 2 else (6 if lag <= 5 else 2)

    pos = 0
    g = ctx.get("gain_since_bsp", 0.0)
    pos += 8 if g <= 0.03 else (4 if g <= 0.08 else 0)
    vr_bsp = ctx.get("vol_ratio_bsp")
    vr_now = ctx.get("vol_ratio_now")
    if any(t in ("1", "1p") for t in types):
        pos += 4 if (vr_bsp is not None and vr_bsp < 0.8) else 2
    else:
        pos += 4 if (vr_now is not None and vr_now > 1.2) else 2
    above = ctx.get("above_ma60")
    if any(t in ("2", "2s", "3a", "3b") for t in types):
        pos += 3 if above else 0
    else:
        pos += 3 if above is False else 1
    d["位置"] = pos

    total = round(sum(d.values()), 1)
    return min(100.0, total), d
