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
DOWNLOAD_WORKERS = int(os.environ.get("CHAN_DESK_DL_WORKERS", 10))  # baostock 下载进程数
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
