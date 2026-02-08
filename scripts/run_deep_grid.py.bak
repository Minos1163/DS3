import os
import sys
import csv
import argparse
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_15m30d_v2 import ConservativeBacktester


def run_grid(mode='quick'):
    if mode == 'quick':
        take_profits = [0.10, 0.14]
        stop_losses = [0.006, 0.008]
        cooldowns = [12]
        positions = [0.25, 0.30, 0.50]
        trailing_starts = [0.03]
        trailing_stops = [0.04]
    else:
        take_profits = [0.08,0.10,0.12,0.14,0.16]
        stop_losses = [0.004,0.006,0.008,0.010]
        cooldowns = [6,12,24]
        positions = [0.20,0.25,0.30,0.40,0.50]
        trailing_starts = [0.02,0.03]
        trailing_stops = [0.03,0.04]

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_file = f'logs/deep_grid_{mode}_{timestamp}.csv'
    os.makedirs('logs', exist_ok=True)

    header = ['tp','sl','cooldown','position','trail_start','trail_stop','final_capital','total_trades','win_rate','max_drawdown']
    with open(out_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(header)

        total = len(take_profits)*len(stop_losses)*len(cooldowns)*len(positions)*len(trailing_starts)*len(trailing_stops)
        print(f'Grid mode={mode} — total runs: {total}')
        count = 0
        for tp in take_profits:
            for sl in stop_losses:
                for cd in cooldowns:
                    for pos in positions:
                        for ts in trailing_starts:
                            for tsp in trailing_stops:
                                count += 1
                                print(f'\n[{count}/{total}] Running TP={tp}, SL={sl}, CD={cd}, POS={pos}, TS={ts}, TSP={tsp}')
                                bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
                                bt.take_profit_pct = tp
                                bt.stop_loss_pct = sl
                                bt.cooldown_bars = cd
                                bt.position_percent = pos
                                bt.trailing_start_pct = ts
                                bt.trailing_stop_pct = tsp
                                bt.max_consecutive_losses = 2
                                bt.volume_quantile = 0.30
                                bt.short_volume_quantile = 0.45
                                bt.max_hold_bars = 60

                                df = bt.load_data('data/SOLUSDT_15m_120d.csv')
                                if df is None:
                                    print('Missing data file: data/SOLUSDT_15m_120d.csv')
                                    return
                                df = bt.calculate_indicators(df)
                                bt.run_backtest(df)
                                # collect stats
                                total_trades = len(bt.trades)
                                winning = len([t for t in bt.trades if t['pnl'] > 0])
                                win_rate = (winning / total_trades * 100) if total_trades>0 else 0
                                import pandas as pd
                                if bt.trades:
                                    dft = pd.DataFrame(bt.trades)
                                    dft['cumulative_capital'] = 100.0 + dft['pnl'].cumsum()
                                    dft['peak'] = dft['cumulative_capital'].cummax()
                                    dft['dd'] = (dft['peak'] - dft['cumulative_capital']) / dft['peak']
                                    max_dd = dft['dd'].max() * 100
                                else:
                                    max_dd = 0
                                writer.writerow([tp, sl, cd, pos, ts, tsp, f"{bt.capital:.2f}", total_trades, f"{win_rate:.2f}", f"{max_dd:.2f}"])
                                f.flush()
    print(f'Grid complete. Results saved to {out_file}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['quick','full'], default='quick')
    args = parser.parse_args()
    run_grid(mode=args.mode)
