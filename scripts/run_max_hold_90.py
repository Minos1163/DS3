import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_15m30d_v2 import ConservativeBacktester


def main():
    backtester = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    # set max_hold_bars to 90 for this experiment
    backtester.max_hold_bars = 90
    print(f"设置 max_hold_bars = {backtester.max_hold_bars}")

    df = backtester.load_data("data/SOLUSDT_15m_30d.csv")
    if df is None:
        return
    df = backtester.calculate_indicators(df)
    backtester.run_backtest(df)
    backtester.analyze_results()


if __name__ == '__main__':
    main()
