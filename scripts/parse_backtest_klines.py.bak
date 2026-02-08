"""Parse backtest K-line log, extract closed trades, produce CSV and equity curve PNG.

Usage:
    python scripts/parse_backtest_klines.py

It will auto-find the most recent files under logs/ named backtest_klines_*.txt and
backtest_summary_*.txt and produce:
  - logs/trade_log_parsed_<ts>.csv
  - logs/equity_curve_<ts>.png (if matplotlib available)
"""
from __future__ import annotations

import glob
import os
import re
import sys
from datetime import datetime

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover
    print("请先安装 pandas: pip install pandas")
    raise


def find_latest(pattern: str) -> str | None:
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort()
    return files[-1]


def parse_summary(summary_path: str) -> dict:
    info = {}
    with open(summary_path, "r", encoding="utf-8") as f:
        txt = f.read()
    m = re.search(r"交易对:\s*(\S+)", txt)
    if m:
        info["symbol"] = m.group(1)
    m = re.search(r"初始资金:\s*([0-9.,]+)", txt)
    if m:
        info["initial_capital"] = float(m.group(1).replace(",", ""))
    else:
        info["initial_capital"] = 10000.0
    return info


def parse_klines(klines_path: str) -> list[dict]:
    trades = []
    pnl_re = re.compile(r"盈亏\s*([+-]?\d+(?:\.\d+)?)")
    time_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

    with open(klines_path, "r", encoding="utf-8") as f:
        for line in f:
            if "平仓" in line and "盈亏" in line:
                t = None
                mtime = time_re.search(line)
                if mtime:
                    t = mtime.group(1)
                mp = pnl_re.search(line)
                if not mp:
                    continue
                pnl = float(mp.group(1))
                # normalize action
                action = "CLOSE"
                trades.append({"timestamp": t or "", "action": action, "pnl": pnl, "raw_line": line.strip()})
    return trades


def main():
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    klines_path = find_latest(os.path.join(logs_dir, "backtest_klines_*.txt"))
    summary_path = find_latest(os.path.join(logs_dir, "backtest_summary_*.txt"))
    if not klines_path or not summary_path:
        print("未找到回测日志或汇总文件，请确认 logs/ 目录下存在 backtest_klines_*.txt 与 backtest_summary_*.txt")
        sys.exit(1)

    info = parse_summary(summary_path)
    trades = parse_klines(klines_path)

    ts = os.path.splitext(os.path.basename(klines_path))[0].replace("backtest_klines_", "")

    if not trades:
        print("未从 K-line 日志中解析到平仓记录 (没有包含 '平仓' 和 '盈亏' 的行)。")
        sys.exit(1)

    # build dataframe
    df = pd.DataFrame(trades)
    df["pnl"] = df["pnl"].astype(float)
    initial = info.get("initial_capital", 10000.0)
    df["cumulative_equity"] = (df["pnl"].cumsum() + initial).round(4)
    df.insert(0, "symbol", info.get("symbol", ""))

    out_csv = os.path.join(logs_dir, f"trade_log_parsed_{ts}.csv")
    df.to_csv(out_csv, index=False, encoding="utf-8")
    print(f"已写入逐笔 CSV: {out_csv}")

    # compute metrics
    total = len(df)
    wins = (df["pnl"] > 0).sum()
    losses = (df["pnl"] <= 0).sum()
    win_rate = wins / total * 100
    gross_profit = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss = -df.loc[df["pnl"] < 0, "pnl"].sum()
    avg_win = df.loc[df["pnl"] > 0, "pnl"].mean() if wins else 0.0
    avg_loss = df.loc[df["pnl"] < 0, "pnl"].mean() if losses else 0.0
    expectancy = df["pnl"].sum() / total
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    print("\n回测逐笔统计:")
    print(f"  交易总数: {total}")
    print(f"  赢利笔数: {wins}")
    print(f"  亏损笔数: {losses}")
    print(f"  胜率: {win_rate:.2f}%")
    print(f"  毛利: {gross_profit:.4f}")
    print(f"  毛损: {gross_loss:.4f}")
    print(f"  收益因子: {pf:.2f}")
    print(f"  期望值(每笔): {expectancy:.4f}")

    # try plotting (use non-interactive Agg backend to avoid GUI/import issues)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.figure(figsize=(8, 4))
        plt.plot(df["cumulative_equity"], marker="o")
        plt.title(f"Equity Curve ({info.get('symbol','')})")
        plt.xlabel("Trade #")
        plt.ylabel("Equity")
        plt.grid(True)
        out_png = os.path.join(logs_dir, f"equity_curve_{ts}.png")
        plt.tight_layout()
        plt.savefig(out_png, dpi=150)
        print(f"已生成权益曲线图: {out_png}")
    except Exception as exc:
        print("绘图失败（可能缺少或导入 matplotlib 出错），已生成 CSV。安装或修复 matplotlib 后可生成图像。", exc)


if __name__ == "__main__":
    main()
