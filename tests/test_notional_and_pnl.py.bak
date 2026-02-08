import sys
import os
import pandas as pd

# ensure project root on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest_15m30d_optimized import OptimizedBacktester


def approx(a, b, tol=1e-8):
    return abs(a - b) <= tol


def run_tests():
    bt = OptimizedBacktester(initial_capital=1000.0, leverage=5.0)
    bt.position_percent = 0.1  # 10%

    # fake row for opening
    row = pd.Series({'close': 100.0, 'rsi': 50.0, 'ema_5': 0, 'ema_20': 0, 'macd_hist': 0, 'volume_quantile': 1.0},
                    name=pd.Timestamp('2026-01-01 00:00'))

    bt.execute_trade(row, 'LONG')

    # checks for notional naming and sizes
    expected_position_notional = 1000.0 * 0.1
    expected_leveraged_notional = expected_position_notional * 5.0

    assert hasattr(bt, 'position_notional')
    assert hasattr(bt, 'leveraged_notional')
    assert approx(bt.position_notional, expected_position_notional)
    assert approx(bt.leveraged_notional, expected_leveraged_notional)

    assert approx(bt.position_size, expected_leveraged_notional / 100.0)

    # simulate close at +10%
    pnl_pct = (110.0 - 100.0) / 100.0
    bt.close_position(110.0, pd.Timestamp('2026-01-01 01:00'), 'TEST', pnl_pct)

    # pnl should be pnl_pct * leveraged_notional
    expected_pnl = pnl_pct * expected_leveraged_notional

    # last trade recorded
    last = bt.trades[-1]
    assert approx(last['pnl'], expected_pnl)

    # state cleared
    assert bt.position is None
    assert bt.position_size == 0.0
    print('DEBUG after close:', getattr(bt, 'position_notional', None), getattr(bt, 'leveraged_notional', None))
    assert approx(bt.position_notional, 0.0)
    assert approx(bt.leveraged_notional, 0.0)

    print('All notional and PnL tests passed')


if __name__ == '__main__':
    run_tests()
