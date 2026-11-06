"""A 股涨跌停幅度。北交所不在数据里，不处理。"""
from .settings import EMO

CHINEXT_REFORM = "2020-08-24"   # 创业板注册制，涨跌幅改为 20%


def limit_pct(code: str, date: str, is_st=False) -> float:
    b = code[:5]
    if b == "sh.68":
        return 0.20
    if b == "sz.30" and date >= CHINEXT_REFORM:
        return 0.20
    return 0.05 if is_st else 0.10


def is_limit_up(code, date, is_st, pct, close, high) -> bool:
    if pct is None or close is None or high is None:
        return False
    return pct >= limit_pct(code, date, is_st) - EMO["zt_tol"] and close >= high * (1 - 1e-6)


def is_limit_down(code, date, is_st, pct, close, low) -> bool:
    if pct is None or close is None or low is None:
        return False
    return pct <= -(limit_pct(code, date, is_st) - EMO["zt_tol"]) and close <= low * (1 + 1e-6)
