"""全局配置。改这里即可调整数据范围、缠论参数和打分权重。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAN_DIR = ROOT / "chan"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "market.db"
PROGRESS_PATH = DATA_DIR / "job_progress.json"
LOG_PATH = DATA_DIR / "job.log"

DATA_DIR.mkdir(exist_ok=True)
if str(CHAN_DIR) not in sys.path:
    sys.path.insert(0, str(CHAN_DIR))

# 设为 1 时使用合成行情（离线测试用）
MOCK = os.environ.get("CHAN_DESK_MOCK") == "1"

# 日线数据起点（缠论计算从这里开始），信号从 SIGNAL_START 起记录（前面作为预热）
DATA_START = os.environ.get("CHAN_DESK_START", "2019-01-01")
SIGNAL_START = os.environ.get("CHAN_DESK_SIGNAL_START", "2020-01-01")

BENCHMARK = "sh.000300"          # 沪深300 作为基准
DOWNLOAD_WORKERS = int(os.environ.get("CHAN_DESK_DL_WORKERS", 5))  # baostock 下载进程数（太多容易被限流卡住）
SCAN_WORKERS = max(1, (os.cpu_count() or 4) - 1)  # 信号计算进程数
MIN_BARS = 120                   # 上市不足该K线数的股票不参与选股

# chan.py 配置（与 chan.py 的 CChanConfig 键一致）
CHAN_CONFIG = {
    "bi_strict": True,
    "seg_algo": "chan",
    "zs_algo": "normal",
    "divergence_rate": float("inf"),
    "min_zs_cnt": 1,
    "bsp1_only_multibi_zs": True,
    "max_bs2_rate": 0.9999,
    "macd_algo": "peak",
    "bs1_peak": False,
    "bs_type": "1,1p,2,2s,3a,3b",
    "bsp2_follow_1": False,
    "bsp3_follow_1": False,
    "print_warning": False,
    "print_err_time": False,
    "kl_data_check": False,
}

# 打分：各类买点的基础分（类型分上限 35）
TYPE_BASE = {"1": 30, "1p": 22, "2": 26, "2s": 20, "3a": 24, "3b": 24}
TYPE_NAME = {"1": "一买", "1p": "盘整一买", "2": "二买", "2s": "类二买", "3a": "三买(a)", "3b": "三买(b)"}
SELL_TYPE_NAME = {"1": "一卖", "1p": "盘整一卖", "2": "二卖", "2s": "类二卖", "3a": "三卖(a)", "3b": "三卖(b)"}

# 交易成本（回测）
COMMISSION = 0.00025   # 佣金（双边）
STAMP_TAX = 0.0005     # 印花税（卖出）
SLIPPAGE = 0.001       # 滑点（双边）

# ---------------------------------------------------------------- 情绪周期（92科比）
# 题材来源：akshare = 东方财富概念板块（失败时自动退回 baostock 证监会行业）；industry = 只用行业
THEME_SOURCE = os.environ.get("CHAN_DESK_THEME", "akshare")
THEME_REFRESH_DAYS = 7           # 题材成分每隔几天重新下载一次
THEME_MAX_MEMBERS = 300          # 成分股太多的“概念”过于宽泛（如融资融券），不当作题材
CUSTOM_THEME_CSV = DATA_DIR / "themes_custom.csv"   # 可选：自己维护的题材，两列 code,theme
# 名称里含这些字的东财概念不是“题材”，而是指数、资金、持股或财务标签
THEME_BLACKLIST = (
    "融资融券", "深股通", "沪股通", "港股通", "标准普尔", "富时罗素", "MSCI", "重仓", "预盈预增", "预亏预减",
    "昨日", "转债标的", "证金持股", "上证", "深证", "中证", "HS300", "央视50", "创业板综", "深成500",
    "茅指数", "宁组合", "破净", "低价股", "百元股", "AH股", "B股", "GDR", "送转", "股权激励", "股权转让",
    "举牌", "ST股", "专精特新", "微盘股", "QFII", "社保", "养老金", "基金", "红利", "长期破净", "贬值受益",
    "AB股", "参股", "富时", "北交所", "注册制次新股", "IPO受益", "增持", "回购",
)

PHASES = ("低位试错", "主升", "高位震荡", "主跌")
# 各阶段的仓位上限（占“最多持仓”的比例）。92科比：主升敢上仓位；高位震荡不能满仓要降仓；主跌不抄老题材
PHASE_CAP = {"低位试错": 0.5, "主升": 1.0, "高位震荡": 0.5, "主跌": 0.2}
# 三种模式分别在哪些阶段做
MODE_PHASES = {"龙头": ("主升", "高位震荡", "低位试错"), "补涨": ("主升", "高位震荡"), "切换": ("主跌", "低位试错")}
MODE_MAX_HOLD = {"龙头": 10, "补涨": 3, "切换": 10}

EMO = {
    "zt_tol": 0.004,            # 涨跌停判定容差（价格四舍五入到分造成的误差）
    "new_stock_days": 5,        # 上市前 N 个交易日不计入涨跌停统计（注册制新股无涨跌幅限制）
    "temp_window": 250,         # 水温 = 各分项在过去 N 个交易日里的分位数平均
    "temp_min_periods": 40,
    "hot": 60, "cold": 38,      # 水温高于 hot 算热，低于 cold 算冷
    "main_min_height": 4,       # 进入主升至少要有 N 连板的空间高度
    "high_board": 5,            # 近 10 日最高板 ≥N 视为高位股
    "mid_board": (4, 6),        # 中位股：近 5 日最高板在这个区间（4进5、5进6）
    "kill_hp_ret": -0.04,       # 高位股当日平均跌幅超过这个值视为“杀高位”
    "min_stay": 2,              # 进入一个阶段后至少停留 N 天，避免来回跳
    "main_rank": 3,             # 5 日涨停数排名前 N 的题材是“主线”
    "main_min_zt5": 5,
    "old_lookback": 40,         # 过去 N 天主升/高位震荡期的主线 = 老题材
    "new_min_zt": 2,            # 新题材：当天至少 N 只涨停
    "new_rank": 10,             # 且过去 old_lookback 天从未进入 5 日涨停数前 N
    "new_overlap": 0.3,         # 且和老题材的成分股重合度（Jaccard）低于这个值
    "bz_max_gain20": 0.25,      # 补涨：涨停前 20 日涨幅低于这个值才算低位
    "bz_leader_board": 3,       # 补涨：同题材龙头至少 N 连板（打出赚钱效应）
    "qh_max_gain60": 0.30,      # 切换：涨停前 60 日涨幅低于这个值（没被炒作过）
}
