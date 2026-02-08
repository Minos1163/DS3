#!/usr/bin/env python3
"""Run cross-sample backtests for each parameter set listed in the top-N CSV produced by the parallel grid.

Usage:
    python scripts/cross_test_topn.py --top_csv logs/deep_grid_parallel_top10_20260202_151848.csv --out logs/cross_top10_results.csv
"""
import os
import sys
import csv
import json
import argparse

import pandas as pd
from datetime import datetime

# attempt to import local backtester; if it fails, add project root to sys.path and retry
try:
    from backtest_15m30d_v2 import ConservativeBacktester
except Exception:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    from backtest_15m30d_v2 import ConservativeBacktester


def apply_params_to_bt(bt: ConservativeBacktester, params: dict):
    # Map expected column names to backtester attributes
    if "position" in params:
        bt.position_percent = float(params["position"])
    if "tp" in params:
        bt.take_profit_pct = float(params["tp"])
    if "sl" in params:
        bt.stop_loss_pct = float(params["sl"])
    if "cooldown" in params:
        bt.cooldown_bars = int(params["cooldown"])
    if "trail_start" in params:
        bt.trailing_start_pct = float(params["trail_start"])
    if "trail_stop" in params:
        bt.trailing_stop_pct = float(params["trail_stop"])


def run_for_top_csv(top_csv, out_csv, data_files=None):
    if data_files is None:
        data_files = [
            "data/SOLUSDT_15m_60d.csv",
            "data/SOLUSDT_15m_30d.csv",
            "data/SOLUSDT_15m_15d.csv",
            "data/SOLUSDT_5m_15d.csv",
        ]

    os.makedirs("logs", exist_ok=True)
    df = pd.read_csv(top_csv)
    # normalize column names
    df.columns = [c.strip() for c in df.columns]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["row_idx", "params", "data_file", "final_capital", "total_trades", "win_rate", "max_drawdown"])

        for idx, row in df.iterrows():
            params = row.to_dict()
            print(f"\n=== Testing param set #{idx} : {params} ===")
            for dfpath in data_files:
                bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
                # apply default config first
                try:
                    with open("config/trading_config.json", "r", encoding="utf-8") as cf:
                        cfg = json.load(cf)
                        # apply common config
                        strat = cfg.get("strategy", {})
                        bt.use_volume_quantile_filter = strat.get(
                            "use_volume_quantile_filter", bt.use_volume_quantile_filter
                        )
                        bt.volume_quantile = strat.get("volume_quantile", bt.volume_quantile)
                        bt.short_volume_quantile = strat.get("short_volume_quantile", bt.short_volume_quantile)
                        bt.use_time_filter = strat.get("use_time_filter", bt.use_time_filter)
                        allowed = strat.get("allowed_hours_utc", None)
                        if allowed is not None:
                            bt.allowed_hours = set(allowed)
                        bt.max_hold_bars = strat.get("max_hold_bars", bt.max_hold_bars)
                        bt.max_consecutive_losses = strat.get("max_consecutive_losses", bt.max_consecutive_losses)
                except Exception:
                    pass

                apply_params_to_bt(bt, params)
                df_klines = bt.load_data(dfpath)
                if df_klines is None:
                    print(f"Missing data file {dfpath}, skipping")
                    continue
                df_klines = bt.calculate_indicators(df_klines)
                bt.run_backtest(df_klines)
                bt.analyze_results()
                total_trades = len(bt.trades)
                winning = len([t for t in bt.trades if t["pnl"] > 0])
                win_rate = (winning / total_trades * 100) if total_trades > 0 else 0
                if bt.trades:
                    dft = pd.DataFrame(bt.trades)
                    dft["cumulative_capital"] = 100.0 + dft["pnl"].cumsum()
                    dft["peak"] = dft["cumulative_capital"].cummax()
                    dft["dd"] = (dft["peak"] - dft["cumulative_capital"]) / dft["peak"]
                    max_dd = dft["dd"].max() * 100
                else:
                    max_dd = 0
                writer.writerow(
                    [
                        idx,
                        json.dumps(params, ensure_ascii=False),
                        dfpath,
                        f"{bt.capital:.2f}",
                        total_trades,
                        f"{win_rate:.2f}",
                        f"{max_dd:.2f}",
                    ]
                )
                f.flush()

    print(f"Cross-test results saved to {out_csv}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top_csv", required=True)
    parser.add_argument("--out", default=f"logs/cross_top_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    args = parser.parse_args()
    run_for_top_csv(args.top_csv, args.out)


if __name__ == "__main__":
    main()
