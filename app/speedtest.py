"""测速：看 baostock 和 akshare（东方财富）在你的网络下各要多久。结果写到 data/speedtest.txt。
用法：双击 测速.bat，或 python -m app.speedtest
"""
import datetime as dt
import socket
import time

from .settings import DATA_DIR

OUT = DATA_DIR / "speedtest.txt"
lines = []


def log(s):
    print(s, flush=True)
    lines.append(s)
    OUT.write_text("\n".join(lines), encoding="utf-8")


def t(name, fn):
    t0 = time.time()
    try:
        r = fn()
        log(f"{name}: OK {time.time() - t0:.1f}s  {r}")
    except Exception as e:  # noqa
        log(f"{name}: FAIL {time.time() - t0:.1f}s  {type(e).__name__}: {str(e)[:200]}")


def main():
    DATA_DIR.mkdir(exist_ok=True)
    socket.setdefaulttimeout(30)
    log(f"测速开始 {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    end = dt.date.today().strftime("%Y-%m-%d")
    # ---- baostock
    try:
        import baostock as bs
        t("baostock 登录", lambda: bs.login().error_msg)
        def qlist():
            rs = bs.query_stock_basic()
            n = 0
            while rs.error_code == "0" and rs.next():
                n += 1
            return f"{n} 条"
        t("baostock 股票列表", qlist)
        for code in ("sh.600000", "sz.000001", "sz.300750"):
            def q(code=code):
                rs = bs.query_history_k_data_plus(code, "date,close", start_date="2019-01-01", end_date=end,
                                                  frequency="d", adjustflag="2")
                n = 0
                while rs.error_code == "0" and rs.next():
                    n += 1
                return f"{n} 根日线"
            t(f"baostock 日线 {code}", q)

        def q5():
            rs = bs.query_history_k_data_plus("sh.600000", "time,close", start_date="2024-03-01",
                                              end_date="2024-03-01", frequency="5", adjustflag="3")
            n = 0
            while rs.error_code == "0" and rs.next():
                n += 1
            return f"{n} 根5分钟线"
        t("baostock 5分钟线", q5)
        bs.logout()
    except ImportError:
        log("baostock 没有安装")
    # ---- akshare
    try:
        import akshare as ak
        for code in ("600000", "000001", "300750"):
            t(f"akshare 日线 {code}", lambda code=code: f"{len(ak.stock_zh_a_hist(symbol=code, period='daily', start_date='20190101', end_date=end.replace('-', ''), adjust='qfq'))} 根日线")
        t("akshare 沪深300", lambda: f"{len(ak.stock_zh_index_daily_em(symbol='sh000300'))} 根")
        t("akshare 股票列表", lambda: f"{len(ak.stock_info_a_code_name())} 只")
        t("akshare 5分钟线", lambda: (lambda d: f"{len(d)} 根，最早 {d.iloc[0, 0] if len(d) else '-'}")(
            ak.stock_zh_a_hist_min_em(symbol="600000", period="5", adjust="")))
        t("akshare 概念板块列表", lambda: f"{len(ak.stock_board_concept_name_em())} 个")
    except ImportError:
        log("akshare 没有安装")
    log(f"测速结束 {dt.datetime.now():%H:%M:%S}")


if __name__ == "__main__":
    main()
