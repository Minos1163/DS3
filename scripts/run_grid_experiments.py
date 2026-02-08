from backtest_15m30d_v2 import ConservativeBacktester

import os
import sys
import csv
from datetime import datetime

# ensure project root is on path so we can import the backtest module when running from scripts/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


OUTPUT_DIR = "logs"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def run_experiment(stop_loss_pct, cooldown_bars):
    bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    # set params (stop_loss_pct expects fraction, e.g. 0.006 for 0.6%)
    bt.stop_loss_pct = stop_loss_pct
    bt.cooldown_bars = cooldown_bars
    bt.max_consecutive_losses = 2

    df = bt.load_data("data/SOLUSDT_15m_30d.csv")
    df = bt.calculate_indicators(df)
    bt.run_backtest(df)
    # after run, compute summary
    trades = bt.trades
    total_trades = len(trades)
    wins = len([t for t in trades if t["pnl"] > 0])
    _losses = len([t for t in trades if t["pnl"] <= 0])
    win_rate = wins / total_trades * 100 if total_trades else 0
    total_pnl = sum(t["pnl"] for t in trades)
    final_capital = bt.capital
    # drawdown compute
    import pandas as pd

    if trades:
        df_tr = pd.DataFrame(trades)
        df_tr["cumulative_capital"] = 100.0 + df_tr["pnl"].cumsum()
        df_tr["peak_capital"] = df_tr["cumulative_capital"].cummax()
        df_tr["drawdown"] = (df_tr["peak_capital"] - df_tr["cumulative_capital"]) / df_tr["peak_capital"]
        max_dd = df_tr["drawdown"].max() * 100
    else:
        max_dd = 0

    return {
        "stop_loss_pct": stop_loss_pct,
        "cooldown_bars": cooldown_bars,
        "final_capital": final_capital,
        "total_pnl": total_pnl,
        "max_drawdown": max_dd,
        "total_trades": total_trades,
        "win_rate": win_rate,
    }


def main():
    stop_losses = [0.005, 0.006, 0.007]
    cooldowns = [8, 12, 24]

    results = []
    for sl in stop_losses:
        for cd in cooldowns:
            print(f"Running SL={sl * 100:.2f}%, cooldown={cd} bars...")
            res = run_experiment(sl, cd)
            print(
                f" -> final {res['final_capital']:.2f} USDT, trades={res['total_trades']}, maxDD={res['max_drawdown']:.2f}%"
            )
            results.append(res)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(OUTPUT_DIR, f"grid_results_{timestamp}.csv")
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "stop_loss_pct",
                "cooldown_bars",
                "final_capital",
                "total_pnl",
                "max_drawdown",
                "total_trades",
                "win_rate",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    print("\nGrid finished. Results saved to", out_file)


if __name__ == "__main__":
    main()
