import os
import sys
import csv
from datetime import datetime

# ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_15m30d_v2 import ConservativeBacktester


def run_grid():
    trailing_start_options = [0.02, 0.03]
    trailing_stop_options = [0.03, 0.04]
    tp_options = [0.10, 0.12, 0.14]
    position_options = [0.25, 0.30, 0.40, 0.50]

    results = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"logs/abc_grid_results_{timestamp}.csv"

    # header for CSV
    header = [
        'trailing_start', 'trailing_stop', 'take_profit', 'position_percent',
        'final_capital', 'total_trades', 'max_drawdown', 'win_rate', 'avg_win', 'avg_loss', 'profit_factor'
    ]

    os.makedirs('logs', exist_ok=True)

    with open(out_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(header)

        for ts in trailing_start_options:
            for tsp in trailing_stop_options:
                for tp in tp_options:
                    for pos in position_options:
                        bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
                        # apply B3-like protections
                        bt.take_profit_pct = tp
                        bt.position_percent = pos
                        bt.use_trailing_stop = True
                        bt.trailing_start_pct = ts
                        bt.trailing_stop_pct = tsp
                        bt.max_consecutive_losses = 2
                        bt.cooldown_bars = 12
                        bt.stop_loss_pct = 0.006
                        bt.volume_quantile = 0.30
                        bt.short_volume_quantile = 0.45
                        bt.max_hold_bars = 60

                        print(f"\n--- Running grid: TS={ts}, TSP={tsp}, TP={tp}, POS={pos} ---")

                        df = bt.load_data('data/SOLUSDT_15m_30d.csv')
                        if df is None:
                            continue
                        df = bt.calculate_indicators(df)
                        bt.run_backtest(df)
                        bt.analyze_results()

                        # gather metrics
                        if bt.trades:
                            df_tr = __import__('pandas').DataFrame(bt.trades)
                            total_trades = len(df_tr)
                            winning_trades = len(df_tr[df_tr['pnl'] > 0])
                            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
                            avg_win = df_tr[df_tr['pnl'] > 0]['pnl'].mean() if winning_trades > 0 else 0
                            avg_loss = abs(df_tr[df_tr['pnl'] < 0]['pnl'].mean()) if len(df_tr[df_tr['pnl'] < 0]) > 0 else 0
                            profit_factor = (avg_win / avg_loss) if avg_loss > 0 else 0
                            # drawdown from trades
                            df_tr['cumulative_capital'] = bt.initial_capital + df_tr['pnl'].cumsum()
                            df_tr['peak_capital'] = df_tr['cumulative_capital'].cummax()
                            df_tr['drawdown'] = (df_tr['peak_capital'] - df_tr['cumulative_capital']) / df_tr['peak_capital']
                            max_dd = df_tr['drawdown'].max() * 100
                        else:
                            total_trades = 0
                            win_rate = 0
                            avg_win = 0
                            avg_loss = 0
                            profit_factor = 0
                            max_dd = 0

                        row = [ts, tsp, tp, pos, f"{bt.capital:.2f}", total_trades, f"{max_dd:.2f}", f"{win_rate:.2f}", f"{avg_win:.2f}", f"{avg_loss:.2f}", f"{profit_factor:.2f}"]
                        writer.writerow(row)
                        f.flush()

    print(f"\nGrid complete. Results saved to: {out_file}")


if __name__ == '__main__':
    run_grid()
