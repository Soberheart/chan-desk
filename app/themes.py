"""题材（概念）成分股。

来源优先级：东方财富概念板块（akshare）→ baostock 证监会行业（兜底）；另外可以在
data/themes_custom.csv 里自己补充题材（两列：code,theme，code 写 600519 或 sh.600519 都行）。

注意：东财概念只能拿到“当前”的成分股，没有历史成分。用它回看过去的题材，会把后来才被
归入某概念的股票也算进去（前视偏差），新概念（例如今年才有的）在历史上会显得“从未被炒作过”。
历史验证和回测里情绪模式的结果要打个折扣看。
"""
import datetime as dt
import time

import pandas as pd

from . import db
from .settings import (CUSTOM_THEME_CSV, MOCK, THEME_BLACKLIST, THEME_MAX_MEMBERS, THEME_REFRESH_DAYS,
                       THEME_SOURCE)


def _std_code(c: str):
    c = str(c).strip().lower()
    if c.startswith(("sh.", "sz.")):
        return c
    c = c.replace("sh", "").replace("sz", "").replace(".", "").zfill(6)
    if c[0] == "6":
        return "sh." + c
    if c[0] in "03":
        return "sz." + c
    return None   # 北交所等不支持


def blacklisted(name: str) -> bool:
    return any(k in name for k in THEME_BLACKLIST)


def fetch_akshare(progress):
    import akshare as ak
    names = ak.stock_board_concept_name_em()[["板块名称", "板块代码"]].values.tolist()
    names = [(n, c) for n, c in names if not blacklisted(str(n))]
    rows, fails = [], 0
    progress("stage", f"下载东财概念成分股（{len(names)} 个概念）")
    for i, (name, bk) in enumerate(names):
        cons = None
        for attempt in range(3):
            try:
                cons = ak.stock_board_concept_cons_em(symbol=bk)
                break
            except Exception:  # noqa
                time.sleep(1 + attempt * 2)
        if cons is None:
            fails += 1
        elif 0 < len(cons) <= THEME_MAX_MEMBERS:
            for c in cons["代码"].astype(str):
                code = _std_code(c)
                if code:
                    rows.append((code, str(name), "东财概念"))
        time.sleep(0.15)
        if (i + 1) % 10 == 0 or i + 1 == len(names):
            progress("themes", done=i + 1, total=len(names))
    if fails > len(names) * 0.3:
        raise RuntimeError(f"东财概念有 {fails}/{len(names)} 个下载失败")
    return rows


def fetch_industry(progress):
    from .datafeed import _BS, _rs_to_df
    progress("stage", "下载 baostock 证监会行业分类")
    bs = _BS.ensure()
    df = _rs_to_df(bs.query_stock_industry())
    df = df[df["industry"].astype(str).str.len() > 0]
    return [(r.code, r.industry, "证监会行业") for r in df.itertuples()]


def fetch_mock(progress):
    from .datafeed import MOCK_THEMES, mock_stock_list, mock_themes_of
    return [(code, MOCK_THEMES[t], "模拟题材") for code, _, _ in mock_stock_list() for t in mock_themes_of(code)]


def load_custom():
    if not CUSTOM_THEME_CSV.exists():
        return []
    df = pd.read_csv(CUSTOM_THEME_CSV, dtype=str)
    if df.shape[1] < 2:
        return []
    rows = []
    for code, theme in df.iloc[:, :2].itertuples(index=False):
        c = _std_code(code) if isinstance(code, str) else None
        if c and isinstance(theme, str) and theme.strip():
            rows.append((c, theme.strip(), "自定义"))
    return rows


def update_themes(progress=lambda *a, **k: None, force=False):
    """按需刷新题材库，返回 (来源, 题材数)。"""
    db.init_db()
    meta = db.get_meta("themes_update") or {}
    want = "mock" if MOCK else THEME_SOURCE
    if not force and meta.get("want") == want and meta.get("date"):
        age = (dt.date.today() - dt.date.fromisoformat(meta["date"])).days
        if age < THEME_REFRESH_DAYS:
            return meta.get("source"), meta.get("themes")
    source, note = None, ""
    if MOCK:
        rows, source = fetch_mock(progress), "模拟题材"
    else:
        rows = []
        if want == "akshare":
            try:
                rows, source = fetch_akshare(progress), "东财概念"
            except ImportError:
                note = "没有安装 akshare，改用 baostock 行业分类（pip install akshare 后重新更新即可）"
            except Exception as e:  # noqa
                note = f"东财概念下载失败（{e}），改用 baostock 行业分类"
            if note:
                progress("stage", note)
        if not rows:
            rows, source = fetch_industry(progress), "证监会行业"
    rows += load_custom()
    with db.session() as c:
        c.execute("DELETE FROM themes")
        c.executemany("INSERT OR IGNORE INTO themes VALUES(?,?,?)", rows)
        n = c.execute("SELECT COUNT(DISTINCT theme) FROM themes").fetchone()[0]
    db.set_meta("themes_update", {"date": dt.date.today().isoformat(), "want": want, "source": source,
                                  "themes": n, "rows": len(rows), "note": note})
    return source, n


def load_membership():
    """返回 {code: [theme, ...]}。"""
    with db.session() as c:
        rows = c.execute("SELECT code, theme FROM themes").fetchall()
    out = {}
    for code, t in rows:
        out.setdefault(code, []).append(t)
    return out
