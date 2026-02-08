"""
Run two DCA rotation backtests (neutral vs current) and print a comparison.
Usage:
  python scripts/run_dca_backtest_compare.py --config1 config/dca_rotation_best_neutral.json --config2 config/dca_rotation_best.json

This script imports the backtest logic from `backtest_dca_rotation.py`.
"""

from backtest_dca_rotation import load_run_config, DCARotationBacktester

import argparse
import os
import sys

# ensure project root is importable (backtest_dca_rotation.py is in project root)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def run_once(config_path: str):
    symbols, interval, days, initial_capital, params = load_run_config(config_path)
    print(f"Loaded params from {config_path}: {params}")
    bt = DCARotationBacktester(
        symbols=symbols, interval=interval, days=days, initial_capital=initial_capital, params=params
    )
    bt.run_backtest()
    metrics = bt.metrics()
    summary = bt.summarize()
    bt.save_results()
    return metrics, summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config1", default="config/dca_rotation_best_neutral.json")
    parser.add_argument("--config2", default="config/dca_rotation_best.json")
    args = parser.parse_args()

    cfg1 = args.config1
    cfg2 = args.config2

    print(f"Running backtest A (neutral) with {cfg1}")
    m1, s1 = run_once(cfg1)
    print("--- Summary A ---")
    print(s1)

    print(f"\nRunning backtest B (current) with {cfg2}")
    m2, s2 = run_once(cfg2)
    print("--- Summary B ---")
    print(s2)

    print("\n--- Metrics comparison (A vs B) ---")
    keys = sorted(set(list(m1.keys()) + list(m2.keys())))
    for k in keys:
        v1 = m1.get(k, None)
        v2 = m2.get(k, None)
        diff = None
        try:
            diff = (v2 - v1) if (v1 is not None and v2 is not None) else None
        except Exception:
            diff = None
        print(f"{k}: A={v1}  B={v2}  delta={diff}")


if __name__ == "__main__":
    main()
