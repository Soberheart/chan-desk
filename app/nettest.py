"""网络诊断：看连国内各行情服务器是否通、多快、和系统代理有没有关系。结果写到 data/nettest.txt。
用法：双击 网络诊断.bat
"""
import datetime as dt
import socket
import time
import urllib.request

from .settings import DATA_DIR

OUT = DATA_DIR / "nettest.txt"
lines = []
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
URLS = [
    ("百度（基准）", "https://www.baidu.com", {}),
    ("东方财富 日线", "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&fields1=f1,f2,f3"
                 "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&beg=20240101&end=20500101&lmt=100", {}),
    ("东方财富 概念板块", "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=20&fs=m:90+t:3&fields=f12,f14", {}),
    ("腾讯 日线", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600000,day,2024-01-01,2026-12-31,500,qfq",
     {}),
    ("新浪 日线", "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh600000"
              "&scale=240&ma=no&datalen=100", {"Referer": "https://finance.sina.com.cn"}),
]


def log(s):
    print(s, flush=True)
    lines.append(s)
    OUT.write_text("\n".join(lines), encoding="utf-8")


def get(url, headers, use_proxy):
    import requests
    s = requests.Session()
    s.trust_env = use_proxy          # False = 不走系统代理 / 环境变量代理
    t0 = time.time()
    try:
        r = s.get(url, headers={**UA, **headers}, timeout=15)
        return f"HTTP {r.status_code}  {len(r.content)} 字节  {time.time() - t0:.1f}s"
    except Exception as e:  # noqa
        return f"失败 {time.time() - t0:.1f}s  {type(e).__name__}: {str(e)[:120]}"


def tcp(host, port):
    t0 = time.time()
    try:
        socket.create_connection((host, port), timeout=10).close()
        return f"连通 {time.time() - t0:.2f}s"
    except Exception as e:  # noqa
        return f"失败 {time.time() - t0:.1f}s  {type(e).__name__}: {e}"


def main():
    DATA_DIR.mkdir(exist_ok=True)
    log(f"网络诊断 {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    px = urllib.request.getproxies()
    log(f"系统/环境代理：{px if px else '无'}")
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        en = winreg.QueryValueEx(k, "ProxyEnable")[0]
        sv = winreg.QueryValueEx(k, "ProxyServer")[0] if en else ""
        log(f"Windows 系统代理开关：{'开 ' + sv if en else '关'}")
    except Exception:  # noqa
        pass
    log(f"baostock 服务器 TCP：{tcp('public-api.baostock.com', 10030)}")
    try:
        ip = socket.gethostbyname("public-api.baostock.com")
        log(f"baostock 服务器解析到：{ip}")
    except Exception as e:  # noqa
        log(f"baostock 域名解析失败：{e}")
    for name, url, h in URLS:
        log(f"{name}｜走系统代理：{get(url, h, True)}")
        if px:
            log(f"{name}｜不走代理：{get(url, h, False)}")
    try:
        import requests
        r = requests.Session()
        r.trust_env = False
        ipinfo = r.get("https://myip.ipip.net", headers=UA, timeout=10).text.strip()
        log(f"不走代理时的出口 IP：{ipinfo[:120]}")
        r2 = requests.get("https://myip.ipip.net", headers=UA, timeout=10).text.strip()
        log(f"走系统代理时的出口 IP：{r2[:120]}")
    except Exception as e:  # noqa
        log(f"查询出口 IP 失败：{e}")
    log("诊断结束")


if __name__ == "__main__":
    main()
