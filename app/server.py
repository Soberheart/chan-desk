"""本地网页服务：python -m app.server  →  http://127.0.0.1:8765"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict

from flask import Flask, jsonify, request, send_from_directory

from . import analytics, datafeed, db
from .chan_engine import Stepper, new_chan, serialize, make_klu, serialize_level, LEVELS
from .settings import LOG_PATH, MOCK, PROGRESS_PATH, ROOT, WEB_DIR

app = Flask(__name__, static_folder=None)
_job = {"proc": None}
_sessions: "OrderedDict[str, dict]" = OrderedDict()
_slock = threading.Lock()


def ok(data=None, **kw):
    return jsonify({"ok": True, "data": data, **kw})


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def _args():
    return request.get_json(silent=True) or {}


def _list(v):
    if not v:
        return None
    if isinstance(v, list):
        return v or None
    return [x for x in str(v).split(",") if x]


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(WEB_DIR, p)


# ---------------------------------------------------------------- 状态 / 任务
def _progress():
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.get("/api/status")
def status():
    with db.session() as c:
        n_stock = c.execute("SELECT COUNT(*) FROM stocks WHERE bars>0").fetchone()[0]
        n_sig = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        rng = c.execute("SELECT MIN(detect_date), MAX(detect_date) FROM signals").fetchone()
    running = _job["proc"] is not None and _job["proc"].poll() is None
    return ok({"stocks": n_stock, "signals": n_sig, "signal_range": rng, "latest_day": analytics.latest_day(),
               "market": db.get_meta("last_market_update"), "build": db.get_meta("last_signal_build"),
               "running": running, "progress": _progress(), "mock": MOCK})


@app.post("/api/update")
def start_update():
    mode = _args().get("mode", "update")
    if _job["proc"] is not None and _job["proc"].poll() is None:
        return err("已有任务在运行")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    _job["proc"] = subprocess.Popen([sys.executable, "-m", "app.jobs", mode], cwd=str(ROOT),
                                    creationflags=flags, env=os.environ.copy())
    time.sleep(0.5)
    return ok({"started": True})


@app.get("/api/log")
def job_log():
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-200:]
    except Exception:
        lines = []
    return ok(lines)


# ---------------------------------------------------------------- 选股
@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return ok([])
    with db.session() as c:
        rows = c.execute("SELECT code, name FROM stocks WHERE (code LIKE ? OR name LIKE ?) AND bars>0 LIMIT 20",
                         (f"%{q}%", f"%{q}%")).fetchall()
    return ok([{"code": r[0], "name": r[1]} for r in rows])


@app.get("/api/select")
def api_select():
    a = request.args
    res = analytics.select(date=a.get("date") or None, window=int(a.get("window", 3)),
                           min_score=float(a.get("min_score", 0)), types=_list(a.get("types")),
                           boards=_list(a.get("boards")), exclude_st=a.get("exclude_st", "1") == "1")
    return ok(res)


def _day_df(code, end=None):
    df = db.load_daily(code, end=end)
    if df.empty:
        raise ValueError(f"本地没有 {code} 的数据")
    return df


@app.get("/api/chart")
def api_chart():
    code = request.args.get("code")
    date = request.args.get("date") or None
    try:
        df = _day_df(code, date)
    except ValueError as e:
        return err(str(e))
    st = Stepper(df)
    st.goto(len(df))
    data = serialize(st.chan, st.levels, windows=(320,))
    with db.session() as c:
        sig = c.execute("SELECT detect_date, bsp_date, is_buy, types, score FROM signals WHERE code=? "
                        "ORDER BY detect_date DESC LIMIT 60", (code,)).fetchall()
    return ok({"chart": data, "date": df["date"].iloc[-1], "signals": [
        {"detect_date": r[0], "bsp_date": r[1], "buy": bool(r[2]), "types": r[3], "score": r[4]} for r in sig]})


@app.get("/api/confirm30")
def api_confirm30():
    """区间套确认：在 30 分钟级别上看最近几天是否也出现了买点。"""
    code = request.args.get("code")
    date = request.args.get("date") or analytics.latest_day()
    days = db.trade_days(end=date)
    if len(days) < 5:
        return err("交易日数据不足")
    start = days[max(0, len(days) - 60)]
    try:
        m = datafeed.ensure_30m(code, start, date)
    except Exception as e:  # noqa
        return err(f"30分钟数据获取失败：{e}")
    if m.empty:
        return err("没有获取到30分钟数据")
    chan = new_chan(("30m",))
    for r in m.itertuples():
        chan.trigger_load({LEVELS["30m"]: [make_klu(r.time, r.open, r.high, r.low, r.close, r.volume)]})
    recent = set(days[-3:])
    hits = [{"time": f"{b.klu.time.year:04}-{b.klu.time.month:02}-{b.klu.time.day:02} "
                     f"{b.klu.time.hour:02}:{b.klu.time.minute:02}", "types": b.type2str(),
             "sure": bool(b.bi.is_sure)}
            for b in chan[0].bs_point_lst.get_latest_bsp(6) if b.is_buy]
    hits = [h for h in hits if h["time"][:10] in recent]
    return ok({"confirmed": bool(hits), "hits": hits, "chart": serialize_level(chan, 0, 240)})


# ---------------------------------------------------------------- 验证 / 回测
@app.post("/api/validate")
def api_validate():
    a = _args()
    res = analytics.validate(a["start"], a["end"], min_score=float(a.get("min_score", 60)),
                             top_n=int(a.get("top_n", 10)), types=_list(a.get("types")),
                             entry=a.get("entry", "close"), boards=_list(a.get("boards")))
    return ok(res)


@app.post("/api/backtest")
def api_backtest():
    a = _args()
    res = analytics.backtest(
        a["start"], a["end"], min_score=float(a.get("min_score", 70)), max_pos=int(a.get("max_pos", 5)),
        types=_list(a.get("types")), stop_pct=float(a.get("stop_pct", 0.08)),
        use_bsp_stop=bool(a.get("use_bsp_stop", True)), take_profit=float(a.get("take_profit", 0) or 0),
        max_hold=int(a.get("max_hold", 20)), exit_on_sell=bool(a.get("exit_on_sell", True)),
        capital=float(a.get("capital", 1_000_000)), boards=_list(a.get("boards")))
    if "error" in res:
        return err(res["error"])
    return ok(res)


# ---------------------------------------------------------------- 逐根回放
@app.post("/api/replay/start")
def replay_start():
    a = _args()
    code, start, multi = a.get("code"), a.get("start"), bool(a.get("multi"))
    try:
        df = _day_df(code)
    except ValueError as e:
        return err(str(e))
    dates = df["date"].tolist()
    import bisect
    si = bisect.bisect_left(dates, start) if start else max(0, len(dates) - 120)
    si = min(si, len(dates) - 1)
    warm = int(a.get("warmup", 250))
    begin = max(0, si - warm)
    sub = df.iloc[begin:].reset_index(drop=True)
    m30 = None
    note = ""
    if multi:
        try:
            m30 = datafeed.ensure_30m(code, sub["date"].iloc[0], sub["date"].iloc[-1])
            if m30.empty:
                note = "没有获取到30分钟数据，只回放日线"
        except Exception as e:  # noqa
            note = f"30分钟数据获取失败（{e}），只回放日线"
            m30 = None
    st = Stepper(sub, m30)
    token = uuid.uuid4().hex[:10]
    with _slock:
        _sessions[token] = {"stepper": st, "lock": threading.Lock(), "code": code}
        while len(_sessions) > 6:
            _sessions.popitem(last=False)
    return ok({"token": token, "dates": sub["date"].tolist(), "pos": si - begin + 1, "levels": list(st.levels),
               "note": note})


@app.post("/api/replay/goto")
def replay_goto():
    a = _args()
    s = _sessions.get(a.get("token"))
    if not s:
        return err("回放会话已过期，请重新开始", 410)
    with s["lock"]:
        st = s["stepper"]
        st.goto(int(a["pos"]))
        data = serialize(st.chan, st.levels, windows=(int(a.get("window", 160)), 320))
        pos = st.pos
    return ok({"pos": pos, "date": st.day["date"].iloc[pos - 1], "chart": data})


def main():
    db.init_db()
    port = int(os.environ.get("CHAN_DESK_PORT", 8765))
    print(f"缠论复盘选股台已启动： http://127.0.0.1:{port}  （关闭此窗口即停止）", flush=True)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
