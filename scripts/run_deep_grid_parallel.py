import argparse

from backtest_15m30d_v2 import ConservativeBacktester

import os
import sys
import csv
from itertools import product
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def worker(params):
    tp, sl, cd, pos, ts, tsp = params
    bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    bt.take_profit_pct = tp
    bt.stop_loss_pct = sl
    bt.cooldown_bars = cd
    bt.position_percent = pos
    bt.trailing_start_pct = ts
    bt.trailing_stop_pct = tsp
    bt.max_consecutive_losses = 2
    bt.volume_quantile = 0.30
    bt.short_volume_quantile = 0.45
    bt.max_hold_bars = 60

    df = bt.load_data("data/SOLUSDT_15m_120d.csv")
    if df is None:
        return (tp, sl, cd, pos, ts, tsp, None, 0, 0.0, 0.0)
    df = bt.calculate_indicators(df)
    bt.run_backtest(df)
    total_trades = len(bt.trades)
    winning = len([t for t in bt.trades if t["pnl"] > 0])
    win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0
    import pandas as pd

    if bt.trades:
        dft = pd.DataFrame(bt.trades)
        dft["cumulative_capital"] = 100.0 + dft["pnl"].cumsum()
        dft["peak"] = dft["cumulative_capital"].cummax()
        dft["dd"] = (dft["peak"] - dft["cumulative_capital"]) / dft["peak"]
        max_dd = dft["dd"].max() * 100
    else:
        max_dd = 0.0
    return (tp, sl, cd, pos, ts, tsp, f"{bt.capital:.2f}", total_trades, round(win_rate, 2), round(max_dd, 2))


def run_parallel(mode="quick", processes=None):
    if mode == "quick":
        take_profits = [0.10, 0.14]
        stop_losses = [0.006, 0.008]
        cooldowns = [12]
        positions = [0.25, 0.30, 0.50]
        trailing_starts = [0.03]
        trailing_stops = [0.04]
    else:
        take_profits = [0.08, 0.10, 0.12, 0.14, 0.16]
        stop_losses = [0.004, 0.006, 0.008, 0.010]
        cooldowns = [6, 12, 24]
        positions = [0.20, 0.25, 0.30, 0.40, 0.50]
        trailing_starts = [0.02, 0.03]
        trailing_stops = [0.03, 0.04]

    combos = list(product(take_profits, stop_losses, cooldowns, positions, trailing_starts, trailing_stops))
    total = len(combos)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"logs/deep_grid_parallel_{mode}_{timestamp}.csv"
    os.makedirs("logs", exist_ok=True)

    header = [
        "tp",
        "sl",
        "cooldown",
        "position",
        "trail_start",
        "trail_stop",
        "final_capital",
        "total_trades",
        "win_rate",
        "max_drawdown",
    ]

    # choose processes
    import multiprocessing

    cpu = multiprocessing.cpu_count()
    if processes is None:
        processes = max(1, cpu - 1)
    processes = min(processes, total)
    print(f"Running parallel grid with {processes} processes, total runs: {total}")

    # use multiprocessing Pool
    from multiprocessing import Pool

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        with Pool(processes=processes) as pool:
            for i, res in enumerate(pool.imap_unordered(worker, combos), start=1):
                writer.writerow(res)
                f.flush()
                if i % 10 == 0 or i == total:
                    print(f"Completed {i}/{total}")

    print(f"Parallel grid complete. Results saved to {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["quick", "full"], default="full")
    parser.add_argument("--procs", type=int, default=None)
    args = parser.parse_args()
    run_parallel(mode=args.mode, processes=args.procs)
