import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_15m30d_v2 import ConservativeBacktester


def main():
    path = 'data/BTCUSDT_15m_120d.csv'
    bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    df = bt.load_data(path)
    if df is None:
        return
    df = bt.calculate_indicators(df)
    bt.run_backtest(df)
    bt.analyze_results()

if __name__ == '__main__':
    main()
