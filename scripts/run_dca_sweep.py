import os
import sys
import csv
import argparse
import time
from datetime import datetime
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_dca_rotation import DCARotationBacktester, DCAParams


def run_sweep(max_runs: int | None = None, progress_every: int = 5):
    symbols = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TON", "TRX", "AVAX"]
    interval = "5m"
    days = 30

    leverages = [10.0]
    take_profits = [0.015, 0.018, 0.02]
    max_positions_list = [4, 6, 8]
    rsi_entries = [55.0, 60.0, 65.0]
    score_thresholds = [0.02, 0.04, 0.06]

    combos = list(product(leverages, take_profits, max_positions_list, rsi_entries, score_thresholds))
    if max_runs is not None:
        combos = combos[:max_runs]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"logs/dca_sweep_{timestamp}.csv"
    os.makedirs("logs", exist_ok=True)
    total = len(combos)
    start_ts = time.time()

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "leverage",
            "take_profit_pct",
            "max_positions",
            "rsi_entry",
            "score_threshold",
            "total_return_pct",
            "max_drawdown_pct",
            "total_trades",
            "win_rate_pct",
            "trades_per_day",
        ])

        for idx, (lev, tp, max_pos, rsi_entry, score_th) in enumerate(combos, start=1):
            params = DCAParams(
                leverage=lev,
                take_profit_pct=tp,
                max_positions=max_pos,
                rsi_entry=rsi_entry,
                score_threshold=score_th,
                cooldown_seconds=0,
            )
            bt = DCARotationBacktester(symbols=symbols, interval=interval, days=days, initial_capital=100.0, params=params)
            bt.run_backtest()
            m = bt.metrics()
            writer.writerow([
                lev,
                tp,
                max_pos,
                rsi_entry,
                score_th,
                round(m["total_return_pct"], 2),
                round(m["max_drawdown_pct"], 2),
                int(m["total_trades"]),
                round(m["win_rate_pct"], 2),
                round(m["trades_per_day"], 2),
            ])
            f.flush()
            if idx % progress_every == 0 or idx == total:
                elapsed = time.time() - start_ts
                avg = elapsed / idx
                eta = avg * (total - idx)
                print(f"Progress {idx}/{total} | elapsed {elapsed:.1f}s | ETA {eta:.1f}s")

    print(f"✅ Sweep完成: {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-runs", type=int, default=None, help="限制运行组合数量")
    parser.add_argument("--progress-every", type=int, default=5, help="进度打印间隔")
    args = parser.parse_args()
    run_sweep(max_runs=args.max_runs, progress_every=args.progress_every)
