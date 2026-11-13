"""后台任务：更新行情 + 全市场逐根回放生成时点信号。

命令行：python -m app.jobs update        （下载数据、刷新题材、重算缠论信号和情绪周期）
       python -m app.jobs signals       （只重算缠论信号和情绪周期）
       python -m app.jobs emotion       （只重算情绪周期）
       python -m app.jobs themes        （强制重新下载题材成分，再重算情绪周期）
       python -m app.jobs seals         （补全分时封板数据，再重算连板天梯；腾讯/同花顺来源只有最近一两个月）
"""
import datetime as dt
import json
import re
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import datafeed, db, emotion, ladder, themes
from .settings import LOG_PATH, PROGRESS_PATH, SCAN_WORKERS, SIGNAL_START


class Progress:
    def __init__(self):
        self.state = {"running": True, "stage": "", "done": 0, "total": 0,
                      "started": dt.datetime.now().strftime("%H:%M:%S"), "msg": ""}
        self.t0 = time.time()
        self.flush()

    def __call__(self, kind, msg=None, done=None, total=None):
        if kind == "stage":
            self.state.update(stage=msg, done=0, total=0)
            self.log(msg)
        else:
            self.state.update(done=done, total=total)
        self.flush()

    def log(self, msg):
        line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.state["msg"] = msg

    def flush(self):
        self.state["elapsed"] = int(time.time() - self.t0)
        PROGRESS_PATH.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")

    def finish(self, ok=True, msg=""):
        self.state.update(running=False, ok=ok, msg=msg)
        self.log(msg)
        self.flush()


def _scan_one(code):
    from .chan_engine import scan_signals
    conn = db.connect()
    try:
        df = db.load_daily(code, conn=conn)
    finally:
        conn.close()
    try:
        return code, scan_signals(code, df, SIGNAL_START), None
    except Exception as e:  # noqa
        return code, [], f"{e.__class__.__name__}: {e}"


def build_signals(progress):
    with db.session() as c:
        codes = [r[0] for r in c.execute("SELECT code FROM stocks WHERE bars>=60 ORDER BY code")]
    progress("stage", f"逐根回放计算缠论信号（{len(codes)} 只，{SCAN_WORKERS} 进程）")
    conn = db.connect()
    # 写入临时表，算完再整体替换，计算期间网页仍可使用旧信号
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='signals'").fetchone()[0]
    conn.execute("DROP TABLE IF EXISTS signals_new")
    conn.execute(re.sub(r"CREATE TABLE[^(]*\(", "CREATE TABLE signals_new(", schema, count=1))
    conn.commit()
    errors = []
    done = 0
    sql = "INSERT OR REPLACE INTO signals_new VALUES(" + ",".join(["?"] * 19) + ")"
    with ProcessPoolExecutor(SCAN_WORKERS) as ex:
        futs = [ex.submit(_scan_one, c) for c in codes]
        for f in as_completed(futs):
            code, rows, err = f.result()
            done += 1
            if err:
                errors.append(f"{code}: {err}")
            elif rows:
                conn.executemany(sql, rows)
            if done % 25 == 0 or done == len(codes):
                conn.commit()
                progress("scan", done=done, total=len(codes))
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("DROP TABLE signals")
    conn.execute("ALTER TABLE signals_new RENAME TO signals")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sig_detect ON signals(detect_date, is_buy)")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    db.set_meta("last_signal_build", {"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                      "signals": n, "errors": errors[:50]})
    return n, errors


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "update"
    LOG_PATH.write_text("", encoding="utf-8")
    p = Progress()
    try:
        db.init_db()
        msg = []
        if cmd == "update":
            latest, errs = datafeed.update_market(p)
            p.log(f"行情更新完成，最新交易日 {latest}，失败 {len(errs)} 只")
        if cmd in ("update", "themes"):
            try:
                src, nt = themes.update_themes(p, force=cmd == "themes")
                p.log(f"题材库：{src}，{nt} 个题材")
            except Exception as e:  # noqa  题材失败不影响缠论部分
                p.log(f"题材更新失败：{e}")
        if cmd in ("update", "signals"):
            n, errs = build_signals(p)
            msg.append(f"共 {n} 条缠论信号，计算失败 {len(errs)} 只")
        ctx = emotion.build(p)
        last = (db.get_meta("last_emotion_build") or {}).get("phase")
        msg.append(f"情绪周期已更新（当前阶段：{last or '数据不足'}，{ctx['n_picks'] if ctx else 0} 条情绪选股记录）")
        if ctx:
            pairs = ladder.seal_pairs(ctx, last_days=5)                       # 最近 5 天全部涨停
            if cmd == "seals":
                pairs += ladder.seal_pairs(ctx, since=SIGNAL_START, min_board=2)   # 历史二板及以上
            if cmd in ("update", "seals"):
                ok_, bad = datafeed.update_seals(pairs, p)
                if ok_ or bad:
                    p.log(f"分时封板数据：新增 {ok_} 条，拿不到 {bad} 条")
            n = ladder.build(ctx, p)
            ev = (db.get_meta("ladder_eval") or {}).get("二板", {})
            if ev.get("auc"):
                msg.append(f"连板天梯已更新（二进三模型样本外 AUC {ev['auc']:.2f}）")
            else:
                msg.append(f"连板天梯已更新（{n} 条）")
        p.finish(True, "完成：" + "；".join(msg))
    except Exception as e:  # noqa
        p.log(traceback.format_exc())
        p.finish(False, f"出错：{e}")
        raise


if __name__ == "__main__":
    main(sys.argv)
