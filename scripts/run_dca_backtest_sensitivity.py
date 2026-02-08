"""
Run sensitivity test with extreme multipliers and analyze entry times by hour/date and regime.
"""

from backtest_dca_rotation import load_run_config, DCARotationBacktester

from collections import defaultdict

import pandas as pd

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def run_with_overrides(config_path: str, overrides: dict):
    symbols, interval, days, initial_capital, params = load_run_config(config_path)
    # apply overrides to params object attributes if exist, otherwise set attribute
    for k, v in overrides.items():
        setattr(params, k, v)
    print(f"Running with params: {params}")
    bt = DCARotationBacktester(
        symbols=symbols, interval=interval, days=days, initial_capital=initial_capital, params=params
    )
    bt.run_backtest()
    # persist results and candidate logs to logs/ for later inspection
    try:
        bt.save_results()
    except Exception:
        pass
    return bt


def analyze_trades(bt: DCARotationBacktester):
    trades = bt.trades
    by_hour = defaultdict(lambda: defaultdict(int))  # hour -> side -> count
    by_regime_hour = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # date -> hour -> regime_side -> count
    for t in trades:
        entry_time = t.get("entry_time")
        if entry_time is None:
            continue
        # ensure pandas Timestamp
        ts = pd.to_datetime(entry_time)
        hour = int(ts.hour)
        date = ts.date().isoformat()
        side = t.get("direction", bt.params.direction)
        symbol = t.get("symbol")
        if symbol is None:
            continue
        # get regime at that timestamp from data
        df = bt.data.get(symbol)
        regime = "NEUTRAL"
        if df is not None and ts in pd.DatetimeIndex(df.index):
            row = bt._row_at_timestamp(df, ts)
            regime = bt._detect_market_regime(row)
        key = f"{side}"
        by_hour[hour][key] += 1
        by_regime_hour[date][hour][f"{regime}_{side}"] += 1
    return by_hour, by_regime_hour


def print_analysis(title, by_hour, by_regime_hour):
    print(f"\n== {title} ==")
    print("Trades by hour (total by side):")
    for hour in sorted(by_hour.keys()):
        counts = by_hour[hour]
        print(f"{hour:02d}: ", dict(counts))
    print("\nTrades by date/hour/regime:")
    for date in sorted(by_regime_hour.keys()):
        print(f"Date: {date}")
        for hour in sorted(by_regime_hour[date].keys()):
            print(f"  {hour:02d}: ", dict(by_regime_hour[date][hour]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/dca_rotation_best.json")
    args = parser.parse_args()

    neutral_cfg = "config/dca_rotation_best_neutral.json"
    current_cfg = args.config

    # run neutral
    bt_neutral = run_with_overrides(neutral_cfg, {})
    by_hour_n, by_regime_hour_n = analyze_trades(bt_neutral)
    print_analysis("Neutral", by_hour_n, by_regime_hour_n)

    # run extreme overrides
    overrides = {
        "bull_short_threshold_mult": 2.0,
        "bear_long_threshold_mult": 1.5,
    }
    bt_extreme = run_with_overrides(current_cfg, overrides)
    by_hour_e, by_regime_hour_e = analyze_trades(bt_extreme)
    print_analysis("Extreme", by_hour_e, by_regime_hour_e)

    # quick comparison by hour
    print("\n== Hourly comparison (extreme - neutral) by side ==")
    hours = sorted(set(list(by_hour_n.keys()) + list(by_hour_e.keys())))
    for h in hours:
        sides = set(list(by_hour_n.get(h, {}).keys()) + list(by_hour_e.get(h, {}).keys()))
        diffs = {s: by_hour_e.get(h, {}).get(s, 0) - by_hour_n.get(h, {}).get(s, 0) for s in sides}
        print(f"{h:02d}: {diffs}")


if __name__ == "__main__":
    main()
