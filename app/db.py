"""SQLite 本地行情与信号库。"""
import json
import sqlite3
from contextlib import contextmanager

import pandas as pd

from .settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks(
    code TEXT PRIMARY KEY, name TEXT, ipo_date TEXT, is_st INTEGER DEFAULT 0,
    last_date TEXT, bars INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kline_d(
    code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, turn REAL, PRIMARY KEY(code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kline_30m(
    code TEXT, time TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY(code, time)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_d(
    code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, PRIMARY KEY(code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trade_days(date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS signals(
    code TEXT, detect_date TEXT, bsp_date TEXT, is_buy INTEGER, types TEXT,
    is_sure INTEGER, bsp_price REAL, detect_close REAL, stop_price REAL,
    score REAL, detail TEXT,
    ret5 REAL, ret10 REAL, ret20 REAL, oret5 REAL, oret10 REAL, oret20 REAL,
    mdd10 REAL, mfe10 REAL,
    PRIMARY KEY(code, bsp_date, is_buy, detect_date)
);
CREATE INDEX IF NOT EXISTS ix_sig_detect ON signals(detect_date, is_buy);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS themes(code TEXT, theme TEXT, source TEXT, PRIMARY KEY(code, theme)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS emo_daily(
    date TEXT PRIMARY KEY, phase TEXT, temp REAL, temp_raw REAL, cap REAL,
    zt INTEGER, dt INTEGER, zb INTEGER, zb_rate REAL, height INTEGER, lb INTEGER,
    promo1 REAL, promo_lb REAL, prev_zt_ret REAL, prev_lb_ret REAL, damian INTEGER,
    hp_ret REAL, mid_ret REAL, kill INTEGER, up_ratio REAL, amount REAL,
    note TEXT, ladder TEXT, mainline TEXT, old_themes TEXT
);
CREATE TABLE IF NOT EXISTS emo_theme(
    date TEXT, theme TEXT, zt INTEGER, zt5 INTEGER, heat INTEGER, rank5 INTEGER,
    leader TEXT, leader_board INTEGER, is_old INTEGER, is_new INTEGER, PRIMARY KEY(date, theme)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS emo_picks(
    date TEXT, mode TEXT, code TEXT, theme TEXT, board INTEGER, score REAL, fit INTEGER, detail TEXT,
    buyable INTEGER, oret1 REAL, oret3 REAL, oret5 REAL, oret10 REAL, mdd5 REAL,
    PRIMARY KEY(date, mode, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS zt_seal(
    code TEXT, date TEXT, first_seal TEXT, last_seal TEXT, open_times INTEGER, PRIMARY KEY(code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ladder(
    date TEXT, code TEXT, kind TEXT, board INTEGER, theme TEXT, tags TEXT, prob REAL, why TEXT,
    nxt_zt INTEGER, nxt_pct REAL, dab_ret REAL, PRIMARY KEY(date, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dk_holdings(
    code TEXT PRIMARY KEY, cost REAL, buy_date TEXT, position REAL, role TEXT, stop REAL, peers TEXT, theme TEXT,
    note TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS dk_plan(
    date TEXT PRIMARY KEY, phase TEXT, cap TEXT, add_signal TEXT, cut_signal TEXT, note TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS dk_journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, code TEXT, action TEXT, price REAL, position REAL,
    reason TEXT, phase TEXT, temp REAL, verdict TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS sig_emo(
    code TEXT, date TEXT, phase TEXT, theme TEXT, theme_old INTEGER, theme_new INTEGER,
    hyped INTEGER, maxb5 INTEGER, PRIMARY KEY(code, date)
) WITHOUT ROWID;
"""

# 旧库升级：kline_d 新增逐日涨跌幅与 ST 标记（空值的股票会在下次更新时整只重新下载）
MIGRATIONS = {"kline_d": [("pct_chg", "REAL"), ("is_st", "INTEGER")]}


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with session() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def set_meta(key, value):
    with session() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def get_meta(key, default=None):
    with session() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def load_daily(code, end=None, conn=None) -> pd.DataFrame:
    sql = "SELECT date,open,high,low,close,volume,amount,turn FROM kline_d WHERE code=?"
    args = [code]
    if end:
        sql += " AND date<=?"
        args.append(end)
    sql += " ORDER BY date"
    if conn is None:
        with session() as c:
            return pd.read_sql_query(sql, c, params=args)
    return pd.read_sql_query(sql, conn, params=args)


def load_30m(code, start=None, end=None) -> pd.DataFrame:
    sql = "SELECT time,open,high,low,close,volume FROM kline_30m WHERE code=?"
    args = [code]
    if start:
        sql += " AND time>=?"
        args.append(start)
    if end:
        sql += " AND time<=?"
        args.append(end + " 23:59")
    with session() as c:
        return pd.read_sql_query(sql + " ORDER BY time", c, params=args)


def trade_days(start=None, end=None):
    sql, args = "SELECT date FROM trade_days WHERE 1=1", []
    if start:
        sql += " AND date>=?"
        args.append(start)
    if end:
        sql += " AND date<=?"
        args.append(end)
    with session() as c:
        return [r[0] for r in c.execute(sql + " ORDER BY date", args)]


def stock_names():
    with session() as c:
        return {r[0]: r[1] for r in c.execute("SELECT code,name FROM stocks")}
