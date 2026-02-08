#!/usr/bin/env python3
"""Generate equity curve charts for top-N parameter sets on a chosen sample (default: SOL 30d).

Usage:
    python scripts/generate_topn_charts.py --top_csv logs/deep_grid_parallel_top10_20260202_151848.csv --sample data/SOLUSDT_15m_30d.csv
"""
import os
import sys
import json
import argparse
import pandas as pd
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from backtest_15m30d_v2 import ConservativeBacktester
except Exception:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    from backtest_15m30d_v2 import ConservativeBacktester

def load_top_csv(path):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    return df


def apply_params(bt: ConservativeBacktester, params: dict):
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


def plot_equity_for_top(df_top, sample_file, out_dir="reports/plots"):
    os.makedirs(out_dir, exist_ok=True)
    for idx, row in df_top.iterrows():
        params = row.to_dict()
        bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
        # apply common config
        try:
            with open("config/trading_config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
                strat = cfg.get("strategy", {})
                bt.use_time_filter = strat.get("use_time_filter", bt.use_time_filter)
                bt.allowed_hours = set(strat.get("allowed_hours_utc", bt.allowed_hours))
                bt.use_volume_quantile_filter = strat.get("use_volume_quantile_filter", bt.use_volume_quantile_filter)
                bt.volume_quantile = strat.get("volume_quantile", bt.volume_quantile)
        except Exception:
            pass
        apply_params(bt, params)
        dfkl = bt.load_data(sample_file)
        if dfkl is None:
            print(f"Sample file {sample_file} missing, skipping")
            continue
        dfkl = bt.calculate_indicators(dfkl)
        bt.run_backtest(dfkl)
        # build equity series from trades
        if bt.trades:
            dft = pd.DataFrame(bt.trades)
            dft["cum"] = 100.0 + dft["pnl"].cumsum()
            plt.figure(figsize=(8, 4))
            plt.plot(dft["exit_time"] if "exit_time" in dft.columns else dft.index, dft["cum"], marker="o")
            plt.title(f"Top#{idx} equity (final {bt.capital:.2f})")
            plt.xlabel("trade")
            plt.ylabel("capital")
            fname = os.path.join(out_dir, f"top{idx}_equity_{int(bt.capital * 100)}.png")
            plt.grid(True)
            plt.tight_layout()
            plt.savefig(fname)
            plt.close()
            print(f"Saved equity chart: {fname}")
        else:
            print(f"No trades for top#{idx}, skipping chart")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top_csv", required=True)
    parser.add_argument("--sample", default="data/SOLUSDT_15m_30d.csv")
    args = parser.parse_args()
    df_top = load_top_csv(args.top_csv)
    plot_equity_for_top(df_top, args.sample)


if __name__ == "__main__":
    main()
